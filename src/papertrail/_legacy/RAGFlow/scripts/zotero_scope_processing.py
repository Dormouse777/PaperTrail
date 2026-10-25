"""Finite continuation of an audited Zotero scope through existing adapters.

All dispatches are charged before sending. Checkpoints retain unknown writes;
the standard import reconciler and attachment planner perform readback first.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from urllib.parse import urlsplit, parse_qs

from workflow_configuration import default_path, local_setting
from zotero_scope_workflow import ROOT, read, sha, scope_digest
from zotero_scope_online import AuditBudget, AuditStop, atomic_json, _query_cost
from zotero_scope_materials import OfficialSources
from paper_bundle import ensure_bundle, record_main_pdf, read_bundle, write_bundle, file_identity
from paper_supplements import acquire_supplements
from zotero_local_api import ZoteroLocalClient
from zotero_filing import file_existing_parent_bundle
from ragflow_prechunk_import import RagflowApi, load_bundle, paper_metadata
from bundle_materials import continue_materials

TOKENIZER = default_path("tokenizer_json")


def runtime_credentials(*, container_name=local_setting("credential_container"),
                        require_retrieval_token=True, environ=None):
    """Prefer a complete caller environment; optional legacy-container fallback.

    Partial/invalid environment configuration fails rather than mixing identities
    with Docker. Pass container_name=None to prohibit any Docker inspection.
    """
    if type(require_retrieval_token) is not bool:
        raise AuditStop("LOCAL_SERVICE_CREDENTIAL_CONFIGURATION_UNAVAILABLE")
    environment = os.environ if environ is None else environ
    names = ("RAGFLOW_API_KEY", "RETRIEVAL_API_TOKEN")
    if any(name in environment for name in names):
        env = {name: environment.get(name, "") for name in names}
    else:
        if (not isinstance(container_name, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", container_name)):
            raise AuditStop("LOCAL_SERVICE_CREDENTIAL_CONFIGURATION_UNAVAILABLE")
        try:
            result = subprocess.run(["docker", "inspect", container_name],
                capture_output=True, text=True, encoding="utf-8", timeout=30)
            if result.returncode:
                raise ValueError
            values = json.loads(result.stdout)[0]["Config"]["Env"]
            if not isinstance(values, list):
                raise ValueError
            env = {}
            for value in values:
                if not isinstance(value, str) or "=" not in value:
                    raise ValueError
                name, token = value.split("=", 1)
                if name in names:
                    if name in env:
                        raise ValueError
                    env[name] = token
        except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, IndexError, TypeError):
            raise AuditStop("LOCAL_SERVICE_CREDENTIAL_CONFIGURATION_UNAVAILABLE") from None
    required = names if require_retrieval_token else names[:1]
    for name in required:
        value = env.get(name)
        if (not isinstance(value, str) or not value or len(value) > 8192
                or any(char.isspace() or ord(char) < 32 for char in value)):
            raise AuditStop("LOCAL_SERVICE_CREDENTIAL_CONFIGURATION_UNAVAILABLE")
    return env["RAGFLOW_API_KEY"], env.get("RETRIEVAL_API_TOKEN", "")


def bounded_writer(scope, budget):
    parents = {p["key"] for p in scope["papers"]}
    children = set()
    class Client(ZoteroLocalClient):
        last_library_version = None
        def _send_once(self, url, method, body, headers, *, timeout=None):
            uri = urlsplit(url)
            path = uri.path.removeprefix("/api")
            if uri.scheme != "http" or uri.netloc != "127.0.0.1:23119":
                raise AuditStop("ZOTERO_DESTINATION_OUT_OF_SCOPE")
            match = re.fullmatch(r"/users/0/items/([A-Z0-9]{8})(/children|/file|/file/view/url)?", path)
            amounts = {"zotero_http_requests": 1}
            allowed = method == "GET" and (path in ("", "/") or match and
                (match[1] in parents and match[2] in (None, "/children") or
                 match[1] in children and match[2] in (None, "/file/view/url")))
            creation = method == "POST" and path == "/users/0/items"
            if creation:
                data = json.loads(body)
                allowed = (isinstance(data, list) and len(data) == 1 and data[0].get("itemType") == "attachment"
                    and data[0].get("parentItem") in parents and data[0].get("linkMode") == "imported_file")
                amounts["zotero_attachment_creations"] = 1
            if method == "POST" and (path == "/local/authorize" or
                    match and match[1] in children and match[2] == "/file" or
                    re.fullmatch(r"/local/uploads/[A-Za-z0-9_-]+", path)):
                allowed = True
            if not allowed:
                raise AuditStop("ZOTERO_WRITE_OUT_OF_SCOPE:" + method + ":" + path)
            if method != "GET":
                amounts["zotero_write_http_requests"] = 1
            event = budget.reserve("zotero-process:" + method + ":" + path, amounts)
            try:
                result = super()._send_once(url, method, body, headers, timeout=timeout)
            except Exception:
                budget.finish(event, failed=True)
                raise
            response_headers = {k.lower(): v for k, v in result[2].items()}
            if method == "GET" and match and match[2] == "/children" and result[0] == 200:
                rows = json.loads(result[1])
                children.update(r["key"] for r in rows if r.get("data", {}).get("parentItem") in parents)
                self.last_library_version = response_headers.get("last-modified-version")
            if creation and result[0] in (200, 201):
                value = json.loads(result[1])
                for row in value.get("successful", {}).values():
                    children.add(row["key"])
            budget.finish(event, response={"status": result[0], "sha256": hashlib.sha256(result[1]).hexdigest()})
            return result
    return Client(timeout=30)


class DownloadTransport:
    def __init__(self, sources):
        self.sources = sources
    def get(self, url, **kwargs):
        if urlsplit(url).scheme != "https" or urlsplit(url).hostname not in self.sources.allowed_hosts:
            raise AuditStop("DOWNLOAD_DESTINATION_OUT_OF_SCOPE")
        budget = self.sources.budget
        checkpoint = self.sources.output / "downloads" / (hashlib.sha256(url.encode()).hexdigest() + ".json")
        if checkpoint.exists() and read(checkpoint).get("failure"):
            raise AuditStop("PREVIOUS_DOWNLOAD_LIMIT_OR_FAILURE_RECORDED")
        event = budget.reserve("download:" + url, {"public_file_downloads": 1})
        try:
            response = self.sources.transport.get(url, **kwargs)
        except Exception:
            budget.finish(event, failed=True)
            raise
        total = 0
        digest = hashlib.sha256()
        failure = None
        eof = False
        class Response:
            status, headers = response.status, response.headers
            def read(self, size=-1):
                nonlocal total, failure, eof
                # Charge before reading: an interrupted stream cannot release bytes.
                maximum = budget.authorization["limits"]["max_single_download_bytes"]
                if size < 0 or size > 1024 * 1024:
                    size = 1024 * 1024
                if total >= maximum:
                    failure = "SINGLE_DOWNLOAD_BYTE_LIMIT"
                    raise AuditStop("SINGLE_DOWNLOAD_BYTE_LIMIT")
                size = min(size, maximum - total)
                reserve = budget.reserve("download-bytes:" + url, {"total_download_bytes": size})
                raw = response.read(size)
                digest.update(raw)
                total += len(raw)
                eof = eof or not raw
                budget.finish(reserve, response={"bytes": len(raw)})
                return raw
            def close(self):
                response.close()
                receipt = {"url": url, "status": response.status, "bytes": total, "sha256": digest.hexdigest(),
                    "eof": eof, "failure": failure or ("RESPONSE_NOT_FULLY_READ" if not eof else None)}
                atomic_json(checkpoint, receipt)
                budget.finish(event, response=receipt)
        return Response()


def bind_inspected_archive(pdf, observation, budget):
    """Migrate the existing adapter's inspected results; never infer from a flag alone."""
    manifest = read_bundle(pdf.parent)
    state = manifest.get("supplements", {})
    pmcid = observation.get("metadata", {}).get("pmcid")
    if not pmcid:
        return
    url = "https://www.ebi.ac.uk/europepmc/webservices/rest/" + pmcid + "/supplementaryFiles"
    events = [e for e in budget.snapshot()["events"] if e["operation"] == "download:" + url and e["status"] == "acknowledged"]
    results = state.get("results", [])
    expected = {r["href"] for r in observation.get("discovered", [])}
    observed = {r.get("filename") for r in results}
    if not events or not expected or not expected <= observed:
        return
    for result in results:
        if result.get("status") in ("downloaded", "exists") and result.get("source") == "europepmc:supplementaryFiles":
            matches = [f for f in manifest["files"] if f.get("relative_path") == result.get("relative_path") and f.get("sha256") == result.get("sha256")]
            if len(matches) != 1 or sha(pdf.parent / matches[0]["relative_path"]) != result["sha256"]:
                raise AuditStop("INSPECTED_ARCHIVE_MATERIAL_DIFFERS")
            result["request_url"] = url
            matches[0]["source_url"] = url
    state["pmc_zip_checked"] = True
    state["results"] = [r for r in results if r.get("filename") in expected]
    state["status"] = "complete" if all(r.get("status") in ("downloaded", "exists") for r in state["results"]) else "partial"
    write_bundle(pdf.parent, manifest)


@contextmanager
def parser_checkpointing(budget, directory):
    """Reuse existing upload/output adapters with a durable job handle."""
    import zotero_mineru_batch as miner
    import mineru_v1_convert as conversion
    old_request, old_parse = miner.request, conversion.parse_batch
    directory = Path(directory)
    def request(url, **kwargs):
        uri = urlsplit(url)
        if uri.scheme != "http" or uri.netloc != "127.0.0.1:8000" or not uri.path.startswith("/v1/"):
            raise AuditStop("PARSER_DESTINATION_OUT_OF_SCOPE")
        event = budget.reserve("mineru:" + kwargs.get("method", "GET") + ":" + uri.path,
                               {"local_parser_http_requests": 1})
        try:
            result = old_request(url, **kwargs)
        except Exception:
            budget.finish(event, failed=True)
            raise
        budget.finish(event, response={"sha256": hashlib.sha256(result[0]).hexdigest()})
        return result
    def parse(candidates, tier, timeout_minutes):
        if len(candidates) != 1:
            raise AuditStop("SINGLE_MATERIAL_PARSE_REQUIRED")
        item = candidates[0]
        source = Path(item["pdf"])
        source_hash = sha(source)
        path = directory / (source_hash + ".json")
        record = read(path) if path.exists() else None
        if record is None:
            from pypdf import PdfReader
            pages = len(PdfReader(source).pages)
            event = budget.reserve("parse-job:" + source_hash,
                {"local_parse_jobs": 1, "total_pdf_pages": pages}, replay_safe=False)
            record = {"source_sha256": source_hash, "source": str(source), "status": "upload_intent", "budget_event": event}
            atomic_json(path, record)
            file_id, observed = miner.upload_pdf(source)
            if observed != source_hash:
                raise AuditStop("PARSER_SOURCE_CHANGED")
            record.update(file_id=file_id, status="uploaded")
            atomic_json(path, record)
        if record["source_sha256"] != source_hash or record["status"] in ("upload_intent", "submit_intent"):
            raise AuditStop("PARSER_DISPATCH_UNCERTAIN_NO_REPLAY")
        item["mineruFileId"] = record["file_id"]
        if not record.get("job_id"):
            record["status"] = "submit_intent";atomic_json(path, record)
            job = miner.post_json(miner.MINERU_BASE + "/v1/parse/jobs", {
                "files": [{"source": {"type": "file_id", "file_id": record["file_id"]}}],
                "tier": tier, "ocr_mode": "auto", "output_formats": ["zip"]})
            record.update(job_id=job["job_id"], status="submitted");atomic_json(path, record)
            budget.finish(record["budget_event"], response={"job_id": record["job_id"]})
        if record.get("result"):
            return record["result"]
        deadline = time.monotonic() + timeout_minutes * 60
        while time.monotonic() < deadline:
            result, _ = miner.get_json(miner.MINERU_BASE + "/v1/parse/jobs/" + record["job_id"])
            if result["status"] in {"completed", "partial", "failed", "canceled"}:
                record.update(status=result["status"], result=result);atomic_json(path, record)
                return result
            time.sleep(2)
        raise TimeoutError("PARSER_JOB_TIMEOUT_RESUME_SAVED_HANDLE")
    miner.request, conversion.parse_batch = request, parse
    try:
        yield
    finally:
        miner.request, conversion.parse_batch = old_request, old_parse


def scoped_index_api(scope, budget, rows, key, transport, *, material_target=None):
    target = scope["target"]
    if material_target is not None:
        keys = ("dataset_id", "base_url", "state_dir")
        approved = [{k: item[k] for k in keys} for item in (scope["target"], scope.get("supplement_target")) if item]
        if material_target not in approved:
            raise AuditStop("SUPPLEMENT_TARGET_REQUIRES_CURRENT_SCOPE_AUTHORIZATION")
        target = material_target
    if transport not in ("http://127.0.0.1:80/api/v1", "http://127.0.0.1:9380/api/v1"):
        raise AuditStop("INDEX_TRANSPORT_OUT_OF_SCOPE")
    dataset, name = target["dataset_id"], rows["document_name"]
    allowed_ids = set()
    class Api(RagflowApi):
        def _json(self, method, path, body=None):
            uri = urlsplit(path)
            base = f"/datasets/{dataset}/documents"
            query = parse_qs(uri.query)
            allowed = (method == "GET" and uri.path == base and query.get("keywords") == [name])
            match = re.fullmatch(re.escape(base) + r"/([A-Za-z0-9]+)(/chunks)?", uri.path)
            if match and match[1] in allowed_ids:
                allowed = (method == "GET" and match[2] == "/chunks" or
                    method == "PATCH" and match[2] is None and body == {"meta_fields": paper_metadata(rows)} or
                    method == "POST" and match[2] == "/chunks" and body.get("content") in rows["by_content"])
            creation = method == "POST" and uri.path == base and query == {"type": ["empty"]} and body == {"name": name}
            if not allowed and not creation:
                raise AuditStop("INDEX_REQUEST_OUT_OF_SCOPE:" + method + ":" + uri.path)
            amounts = {"index_and_readback_http_requests": 1}
            cost = Decimal(0)
            if creation:
                amounts["new_index_documents"] = 1
            if method == "POST" and match and match[2] == "/chunks":
                text = body["content"]
                pricing = dict(budget.authorization)
                pricing["embedding_price_proof"] = read(budget.path.parent / "embedding-price-proof.json")
                tokens, cost = _query_cost(text, 1, pricing)
                amounts.update(new_or_repaired_chunks=1, embedding_text_utf8_bytes=len(text.encode()), embedding_input_tokens=tokens)
            event = budget.reserve("index:" + method + ":" + path, amounts, cost=cost)
            try:
                result = super()._json(method, path, body)
            except Exception:
                budget.finish(event, failed=True)
                raise
            if method == "GET" and uri.path == base:
                allowed_ids.update(d["id"] for d in result["docs"] if d.get("name") == name)
            if creation:
                allowed_ids.add(result["id"])
            budget.finish(event, response=result)
            return result
    return Api(target["base_url"], key, transport_base_url=transport)


def stage_bundle(scope, paper, audit, observation, snapshot, output):
    """Copy only a currently verified original; never rename a Zotero file."""
    candidates = [b for b in audit["body"] if b.get("readable")]
    if not candidates:
        raise ValueError("MAIN_PDF_MISSING_OR_UNREADABLE; resume existing legal acquisition route before parsing")
    if len(candidates) != 1:
        raise ValueError("MULTIPLE_MAIN_VERSIONS_REQUIRE_REVIEW")
    body = candidates[0]
    source = Path(body["path"])
    identity = file_identity(source)
    if identity["sha256"] != body["sha256"]:
        raise AuditStop("CURRENT_PDF_CHANGED")
    matches = [x for x in snapshot["papers"][paper["key"]]["children"]
        if x["key"] == body["attachment_key"] and x["data"].get("parentItem") == paper["key"]]
    if len(matches) != 1 or matches[0]["data"].get("md5") != identity["md5"]:
        raise AuditStop("CURRENT_ATTACHMENT_BYTES_DIFFER")
    old_bundles = audit.get("bundles", [])
    bundle_path = None
    for old in old_bundles:
        candidate = Path(old["path"] if isinstance(old, dict) else old)
        manifest = read_bundle(candidate.parent if candidate.name == "bundle.json" else candidate)
        if manifest and any(f.get("role") == "main_pdf" and f.get("sha256") == body["sha256"] for f in manifest["files"]):
            bundle_path = candidate.parent if candidate.name == "bundle.json" else candidate
            break
    metadata = {"source": "zotero", "id": paper["key"], "doi": paper["doi"], "title": snapshot["papers"][paper["key"]]["parent"]["data"]["title"]}
    if bundle_path is None:
        bundle_path = ensure_bundle(default_path("download_dir", "data/papers") / ("zotero-scope-" + scope_digest(scope)[:12]), metadata)
    destination = bundle_path / source.name
    if not destination.exists():
        shutil.copy2(source, destination)
    if file_identity(destination) != identity:
        raise AuditStop("BUNDLE_COPY_DIFFERS")
    record_main_pdf(bundle_path.parent, metadata, destination, source="zotero")
    manifest = read_bundle(bundle_path)
    saved_binding = manifest.get("zotero", {})
    if saved_binding.get("parent_key") not in (None, paper["key"]):
        raise AuditStop("BUNDLE_ZOTERO_PARENT_DIFFERS")
    manifest.setdefault("zotero", {}).update(parent_key=paper["key"], collection_key=scope.get("collection_key"),
        pdf={"status": "FILED", "attachment_key": body["attachment_key"], **identity})
    if observation.get("eligible_pmc_supplements"):
        evidence = Path(observation["metadata_evidence"])
        manifest["paper"].update(pmcid=observation["metadata"]["pmcid"], supplements_oa_eligible=True,
            supplements_license=observation["license"], pmc_identity_evidence={"path": str(evidence), "sha256": sha(evidence)})
    manifest.setdefault("supplements", {})["official_check"] = observation
    write_bundle(bundle_path, manifest)
    return destination


def normalize_main_candidate(path, pdf, doi):
    """Repair this adapter's early role spelling without re-embedding text."""
    path = Path(path)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows or not all(r["provenance"].get("source_role") == "main_article" for r in rows):
        return
    base = load_bundle(path.with_name("chunks.jsonl"))["rows"]
    copied = json.loads(json.dumps(rows))
    for row, original in zip(copied, base):
        if row["provenance"].get("doi") != doi or row["provenance"].get("source_pdf_sha256") != sha(pdf):
            raise AuditStop("MAIN_ROLE_REPAIR_BINDING_DIFFERS")
        for key in ("doi", "source_role"):
            if key in original["provenance"]:
                row["provenance"][key] = original["provenance"][key]
            else:
                row["provenance"].pop(key, None)
    if copied != base or len(rows) != len(base):
        raise AuditStop("MAIN_ROLE_REPAIR_TEXT_DIFFERS")
    for row in rows:
        row["provenance"]["source_role"] = "main_pdf"
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    manifest = read_bundle(pdf.parent)
    for material in manifest.get("processing", {}).get("materials", []):
        if material.get("chunks_path") == str(path):
            material["chunks_sha256"] = sha(path)
    write_bundle(pdf.parent, manifest)


def process_materials(scope, authorization, output, *, transport, local_only=False):
    output = Path(output)
    budget = AuditBudget(scope, authorization, output / "online-budget.json")
    audit = read(output / "audit.json")
    snapshot = read(output / "identity-snapshot.json")
    observations = {r["key"]: r for r in read(output / "official-materials.json")["papers"]}
    if audit.get("scope_sha256") != scope_digest(scope) or not all(p["identity"] == "verified" for p in audit["papers"]):
        raise AuditStop("LIVE_SCOPE_AUDIT_REQUIRED")
    records = read(output / "processing.json") if (output / "processing.json").exists() else {"scope_sha256": scope_digest(scope), "papers": {}}
    if records["scope_sha256"] != scope_digest(scope):
        raise AuditStop("PROCESSING_SCOPE_CHANGED")
    key = runtime_credentials()[0] if not local_only else None
    writer = bounded_writer(scope, budget)
    writer.discover()
    sources = OfficialSources(scope, authorization, output)
    prepared = ROOT / "RAGFlow/artifacts/prepared"
    parse_root = default_path("bundle_root", "data/bundles") / ("Zotero-Scope-" + scope_digest(scope)[:12])
    for paper in scope["papers"]:
        row = next(r for r in audit["papers"] if r["key"] == paper["key"])
        record = records["papers"].setdefault(paper["key"], {})
        try:
            observation = observations[paper["key"]]
            pdf = stage_bundle(scope, paper, row, observation, snapshot, output)
            record.update(bundle_path=str(pdf.parent / "bundle.json"), source_sha256=sha(pdf))
            atomic_json(output / "processing.json", records)
            if not local_only and observation.get("discovered") and observation.get("eligible_pmc_supplements"):
                bind_inspected_archive(pdf, observation, budget)
                manifest = read_bundle(pdf.parent)
                prior_acquisition = record.get("acquisition", {})
                prior_failed = prior_acquisition.get("supplements_status") == "partial" and not any(
                    r.get("status") in ("downloaded", "exists") for r in prior_acquisition.get("supplements", []))
                if not prior_failed:
                    record["acquisition"] = acquire_supplements(manifest["paper"], pdf,
                        governor=sources.governor, transport=DownloadTransport(sources))
                else:
                    record["acquisition_resume"] = "previous_download_failed_no_automatic_replay; inspect official single-file links"
                manifest = read_bundle(pdf.parent)
                # Compatibility for the already running material-summary reader:
                # this is explicitly a failure label, never an acquired filename.
                for result in manifest.get("supplements", {}).get("results", []):
                    if result.get("status") == "failed" and not result.get("filename"):
                        result.update(filename="未取得附件（文件名未知）", filename_is_display_label=True,
                                      reason_code=result.get("error") or "ACQUISITION_FAILED")
                write_bundle(pdf.parent, manifest)
                record["archive"] = file_existing_parent_bundle(pdf, paper["key"], paper["doi"], scope["collection_key"],
                    client=writer, storage_root=Path(scope["storage_root"]))
                atomic_json(output / "processing.json", records)
            manifest = read_bundle(pdf.parent)
            current_main = [p for p in row["prepared"] if p.get("valid") and p.get("current_pdf_hash_matches")]
            main_path = record.get("main_chunks_path")
            if main_path:
                normalize_main_candidate(main_path, pdf, paper["doi"])
            if main_path is None and current_main:
                main_path = current_main[0]["chunks_path"]
            # Historical correspondence retains its own parse/source identity.
            main_needed = not row["mappings"]
            if main_needed and main_path is None:
                from mineru_v1_convert import convert
                from prepare_chunks import prepare
                with parser_checkpointing(budget, output / "parser-jobs"):
                    converted = convert(pdf, parse_root, execute=True)
                source_id = converted["document_id"]
                result = prepare(Path(converted["target_dir"]), prepared, execute=True, tokenizer_json=TOKENIZER,
                    max_tokens=2000, title_overrides={source_id: {"title": manifest["paper"]["title"], "source": "live_zotero_parent",
                        "source_url": "https://doi.org/" + paper["doi"], "verified_on": audit["created_at"][:10]}},
                    zotero_provenance={"zotero_parent_key": paper["key"], "zotero_pdf_attachment_key": manifest["zotero"]["pdf"]["attachment_key"],
                        "zotero_collection_key": scope["collection_key"], "zotero_server_id": writer.server_id})
                main_path = result["chunks_path"]
                # DOI is explicit in every candidate before indexing.
                path = Path(main_path)
                rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
                for chunk in rows:
                    chunk["provenance"].update(doi=paper["doi"], source_role="main_pdf")
                derived = path.parent / "material-chunks.jsonl"
                raw = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
                if derived.exists() and derived.read_text(encoding="utf-8") != raw:
                    raise AuditStop("MAIN_CHUNKS_CONFLICT")
                derived.write_text(raw, encoding="utf-8")
                main_path = str(derived)
                record["main_chunks_path"] = main_path
                atomic_json(output / "processing.json", records)
            selected = [f["relative_path"] for f in manifest["files"] if f["role"] == "supplement" or main_needed]
            if local_only:
                selected = [pdf.name] if main_needed else []
            with parser_checkpointing(budget, output / "parser-jobs"):
                record["materials"] = continue_materials(pdf, prepared, parse_root, Path(scope["target"]["state_dir"]),
                    scope["target"]["base_url"], scope["target"]["dataset_id"], execute_local=True,
                    execute_ragflow=not local_only, tokenizer_json=TOKENIZER, main_chunks_path=main_path,
                    relative_paths=selected, target_api_factory=(lambda rows, target: scoped_index_api(
                        scope, budget, rows, key, transport, material_target=target)) if not local_only else None)
            if record["materials"].get("write_uncertain"):
                raise AuditStop("INDEX_WRITE_REQUIRES_READBACK_RECONCILIATION")
            record["status"] = "processed"
            record.pop("error", None)
        except AuditStop:
            atomic_json(output / "processing.json", records)
            raise
        except (ValueError, OSError, RuntimeError, KeyError) as error:
            record.update(status="failed", error=str(error))
            if str(error) == "ATTACHMENT_WRITE_UNCERTAIN":
                atomic_json(output / "processing.json", records)
                raise AuditStop("ZOTERO_WRITE_REQUIRES_READBACK_RECONCILIATION") from error
        atomic_json(output / "processing.json", records)
        print("Process " + paper["key"] + ": " + record["status"], flush=True)
    return records


def processing_report(scope, authorization, output, configuration, previous, *, transport, verify_retrieval=False):
    """Refresh local evidence, reuse unchanged readbacks, verify repaired sources."""
    from zotero_scope_workflow import local_audit, local_mappings
    from zotero_scope_online import bounded_index_api, verify_remote_mapping, formal_health, formal_retrieval
    output = Path(output)
    config = read(configuration)
    report = local_audit(scope, config, prepared_root=ROOT / "RAGFlow/artifacts/prepared", paper_root=default_path("download_dir", "data/papers"),
        live_snapshot=read(output / "identity-snapshot.json"))
    report.update(mode="live-processing", generation_calls=0, complete=False, common_stop=None)
    processing = read(output / "processing.json")
    official = {p["key"]: p for p in read(output / "official-materials.json")["papers"]}
    budget = AuditBudget(scope, authorization, output / "online-budget.json")
    mappings, errors = local_mappings(config)
    if errors:
        raise AuditStop("LOCAL_DEFAULT_MAPPING_UNAVAILABLE")
    changed = {p["source_sha256"][:20] for p in processing["papers"].values() if p.get("main_chunks_path")}
    selected = [m for m in mappings if m["document_id"] in changed]
    key, token = runtime_credentials()
    api = bounded_index_api(config, selected, budget, key=key, transport=transport) if selected else None
    if verify_retrieval:
        report["formal_bridge_health"] = formal_health(config, mappings, budget)
    for paper in report["papers"]:
        old = next(p for p in previous["papers"] if p["key"] == paper["key"])
        work = processing["papers"].get(paper["key"], {})
        paper["processing"] = work
        observation = official[paper["key"]]
        paper["supplements"]["official_observation"] = observation
        paper["supplements"]["status"] = observation["status"]
        paper["resume"] = []
        if work.get("error"):
            paper["resume"].append(work["error"])
        if work.get("acquisition_resume"):
            pmcid = observation.get("metadata", {}).get("pmcid")
            url = "https://www.ebi.ac.uk/europepmc/webservices/rest/" + str(pmcid) + "/supplementaryFiles"
            byte_reservations = sum(e["amounts"].get("total_download_bytes", 0) for e in budget.snapshot()["events"]
                if e["operation"] == "download-bytes:" + url)
            paper["resume"].append("既有整包下载失败，禁止盲目重放；" + ("达到64MiB边界，需合法单文件链接或新范围" if byte_reservations >= authorization["limits"]["max_single_download_bytes"] else "从已保存下载记录续办"))
        if observation.get("discovered") and not observation.get("eligible_pmc_supplements"):
            paper["resume"].append("官方元数据缺少可验证补件许可；不以正文可访问代替补件权利核验")
        for mapped in paper["mappings"]:
            mapping = next(m for m in mappings if m["document_id"] == mapped["document_id"] and m["source"]["dataset_id"] == mapped["dataset_id"])
            old_mapping = next((m for m in old["mappings"] if m["document_id"] == mapped["document_id"] and m["dataset_id"] == mapped["dataset_id"]), None)
            if mapped["document_id"] not in changed:
                if old_mapping and old_mapping.get("prepared_exact_matches") == mapped["prepared_exact_matches"]:
                    mapped["remote_readback"] = old_mapping["remote_readback"]
                    mapped["readback_basis"] = "unchanged candidate and original current-session complete readback"
                continue
            if not mapped["prepared_exact_matches"]:
                mapped["remote_readback"] = {"status": "failed", "reason": "LOCAL_CANDIDATE_MAPPING_DIFFERS"}
                continue
            rows = load_bundle(Path(mapped["prepared_exact_matches"][0]))
            mapped["remote_readback"] = verify_remote_mapping(api, mapping, rows)
            if verify_retrieval and mapped["remote_readback"]["status"] == "verified":
                queries_path = output / "verification-queries.json"
                overrides = read(queries_path) if queries_path.exists() else {}
                if overrides and overrides.get("scope_sha256") != scope_digest(scope):
                    raise AuditStop("QUERY_RECOVERY_SCOPE_DIFFERS")
                query = overrides.get("queries", {}).get(paper["key"], {}).get("query") or ("What findings are reported in " + paper["title"] + "?")
                mapped["formal_retrieval"] = formal_retrieval(query, mapping, paper["doi"], config, budget,
                    output / "formal-retrieval", token=token)
                if mapped["formal_retrieval"]["status"] == "verified":
                    paper["default_retrieval"] = "verified"
        if paper["default_retrieval"] != "verified":
            paper["default_retrieval_note"] = "登记已核对；既有来源未重跑未受影响的历史召回验收"
        if not all(isinstance(m["remote_readback"], dict) and m["remote_readback"].get("status") == "verified" for m in paper["mappings"]):
            paper["resume"].append("remote_readback")
        materials = work.get("materials", {}).get("materials", [])
        for material in materials:
            if material.get("status") in ("needs_review", "failed", "unsupported"):
                paper["resume"].append(material["filename"] + ": " + (material.get("reason") or material.get("status")))
        if observation.get("discovered"):
            acquired = {f["filename"] for f in paper["supplements"]["files"] if f.get("hash_verified")}
            absent = [f["href"] for f in observation["discovered"] if f["href"] not in acquired]
            if absent:
                paper["resume"].append("补件尚未取得：" + ", ".join(absent))
        elif observation["status"] != "checked_no_files_in_source":
            paper["resume"].append("官方附件检查未完成：" + str(observation.get("reason")))
        publisher = observation.get("publisher_check", {})
        if publisher.get("status") in ("check_failed", "check_incomplete", "check_unavailable"):
            paper["resume"].append("出版商检查限制：" + str(publisher.get("reason")))
        if publisher.get("article_landing_check"):
            landing = publisher["article_landing_check"]
            paper["resume"].append("实际附件页=" + str(landing.get("status")) + ": " + str(landing.get("error") or "访问拒绝，未检查附件内容"))
        paper["actions"] = (["本次补齐正文解析、增量入库、远端核验及默认召回"] if work.get("main_chunks_path")
            else ["复用已核验正文；补件检查和逐材料续办"])
        if any(m.get("version_evidence", {}).get("status") == "representations_differ" for m in paper["mappings"]):
            paper["actions"] = ["保留已核验旧版本索引；当前PDF版本待判定，未自动复用"]
    ledger = budget.snapshot()
    report["batch_reserved_counts"] = ledger["counts"]
    report["batch_cost_upper_cny"] = ledger["reserved_cost_upper_cny"]
    report["business_writes"] = sum(ledger["counts"].get(k, 0) for k in ("zotero_write_http_requests", "new_index_documents", "new_or_repaired_chunks"))
    report["external_calls"] = sum(sum(e["amounts"].get(k, 0) for k in ("zotero_http_requests", "index_and_readback_http_requests", "public_metadata_or_page_requests", "local_parser_http_requests", "public_file_downloads", "formal_retrieval_queries")) for e in ledger["events"])
    touched = sum(bool(p.get("main_chunks_path") or p.get("archive")) for p in processing["papers"].values())
    report["summary"].update(processed=touched, verified_complete_skipped=0,
        unchanged_main_sources_reused=len(scope["papers"]) - len(changed), unfinished=sum(bool(p["resume"]) for p in report["papers"]),
        remote_verified_papers=sum(bool(p["mappings"]) and all(isinstance(m["remote_readback"], dict) and m["remote_readback"].get("status") == "verified" for m in p["mappings"]) for p in report["papers"]),
        formal_verified_papers=sum(p["default_retrieval"] == "verified" for p in report["papers"]))
    report["functional_status"] = "verified_with_material_limits" if report["summary"]["formal_verified_papers"] == len(changed) else "pending_formal_verification"
    report["material_completeness"] = "partial"
    if (output / "repeat-online.json").exists():
        report["repeat_execution"] = read(output / "repeat-online.json")
    api = None;key = token = ""
    return report
