"""Bounded Zotero gap audit, reused by the existing update-cycle entry point.

Offline mode reads files only. Live identity/index reads require a scope-bound
authorization receipt. Neither mode calls a generation model or repairs data.
Repair decisions must be based on the resulting current audit, not old flags.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit
from pypdf.errors import PyPdfError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Paperdownload"))
# Reuse the project's pinned tokenizer library required by DOCX mapping checks.
from workflow_defaults import default_path
from workflow_configuration import DEFAULT_CONFIGURATION
_tokenizer_runtime = default_path("tokenizer_runtime")
if _tokenizer_runtime is not None:
    sys.path.append(str(_tokenizer_runtime))
from paper_bundle import file_identity, read_bundle
from ragflow_prechunk_import import ImportError_, load_bundle
from ragflow_retrieve import load_mapping


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha(path):
    return file_identity(Path(path))["sha256"]


def scope_digest(scope):
    return hashlib.sha256(json.dumps(scope, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_scope(scope, config):
    papers = scope.get("papers", [])
    if not 1 <= len(papers) <= 10:
        raise ValueError("SCOPE_EXPECTS_1_TO_10_EXPLICIT_PARENTS")
    if len({p["key"] for p in papers}) != len(papers):
        raise ValueError("DUPLICATE_SCOPE_PARENT")
    for paper in papers:
        if not re.fullmatch(r"[A-Z0-9]{8}", paper["key"]) or not paper.get("doi", "").startswith("10."):
            raise ValueError("INVALID_PARENT_OR_DOI")
    collection = scope.get("collection_key")
    if collection and not re.fullmatch(r"[A-Z0-9]{8}", collection):
        raise ValueError("INVALID_COLLECTION")
    target = scope["target"]
    if config.get("scope") != "unified" or not any(
        src.get("confirmed") is True and src["dataset_id"] == target["dataset_id"]
        and src["base_url"].rstrip("/") == target["base_url"].rstrip("/")
        and Path(src["state_dir"]).resolve() == Path(target["state_dir"]).resolve()
        for src in config.get("sources", [])
    ):
        raise ValueError("TARGET_NOT_IN_CONFIRMED_DEFAULT_SCOPE")
    return papers


def validate_authorization(scope, authorization):
    if (authorization.get("scope_sha256") != scope_digest(scope)
            or authorization.get("approved") is not True or authorization.get("closed") is not False):
        raise ValueError("CURRENT_BATCH_AUTHORIZATION_REQUIRED")
    expiry = datetime.fromisoformat(authorization["expires_at"])
    if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
        raise ValueError("BATCH_AUTHORIZATION_EXPIRED")
    return authorization


def pdf_check(path):
    from pypdf import PdfReader
    path = Path(path)
    before = file_identity(path)
    with path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise ValueError("INVALID_PDF_HEADER")
    reader = PdfReader(path)
    if reader.is_encrypted or not len(reader.pages):
        raise ValueError("PDF_ENCRYPTED_OR_EMPTY")
    # Force each page object to be readable; do not claim OCR/figure validation.
    pages = len(reader.pages)
    for page in reader.pages:
        tuple(page.mediabox)
    if file_identity(path) != before:
        raise ValueError("PDF_CHANGED_DURING_AUDIT")
    return {"path": str(path), **before, "pages": pages, "readable": True,
            "scientific_text_and_images_reviewed": False}


def prepared_inventory(root):
    result = []
    for path in Path(root).glob("*/*/manifest.json"):
        try:
            manifest = read(path)
            if not isinstance(manifest, dict):
                raise ValueError("PREPARED_MANIFEST_NOT_OBJECT")
        except (ValueError, OSError) as error:
            manifest = {"inventory_error": str(error), "document_id_hint": path.parent.parent.name}
        result.append((path, manifest))
    return result


def verify_prepared(path, manifest):
    folder = path.parent
    if manifest.get("status") != "PREPARED":
        raise ValueError("PREPARED_STATUS_INVALID")
    for name, field in (("canonical.json", "canonical_sha256"), ("chunks.jsonl", "chunks_sha256")):
        if sha(folder / name) != manifest[field]:
            raise ValueError("PREPARED_ARTIFACT_HASH_DIFFERS:" + name)
    bundle = load_bundle(folder / "chunks.jsonl")
    canonical = read(folder / "canonical.json")
    source_hash = manifest["source_pdf_sha256"]
    if (sha(Path(manifest["source_pdf"])) != source_hash
            or bundle["provenance"].get("source_pdf_sha256") != source_hash
            or canonical.get("provenance", {}).get("sourcePdfSha256") != source_hash
            or bundle["document_id"] != manifest["document_id"]
            or len(bundle["rows"]) != manifest["chunk_count"]):
        raise ValueError("PREPARED_SOURCE_OR_COUNT_DIFFERS")
    for name, expected in manifest.get("parse_artifacts_sha256", {}).items():
        if Path(name).name != name or sha(Path(manifest["bundle_path"]) / name) != expected:
            raise ValueError("PARSE_ARTIFACT_HASH_DIFFERS")
    if not manifest.get("parse_artifacts_sha256"):
        raise ValueError("PARSE_INTEGRITY_NOT_RECORDED")
    return bundle


def local_mappings(config):
    output = []
    errors = []
    for source in config["sources"]:
        if source.get("confirmed") is not True:
            continue
        try:
            _, mapping = load_mapping(Path(source["state_dir"]), source["base_url"], source["dataset_id"], skip_incomplete=True)
            grouped = {}
            for remote_id, entry in mapping.items():
                grouped.setdefault(entry["record"]["document_id"], {})[remote_id] = entry
            for document_id, entries in grouped.items():
                output.append({"source": source, "document_id": document_id, "entries": entries})
        except (ImportError_, ValueError, OSError) as error:
            errors.append({"source": source["name"], "reason": str(error)})
    return output, errors


def load_version_evidence(scope):
    proofs, errors = [], []
    for descriptor in scope.get("version_evidence", []):
        try:
            path = Path(descriptor["path"])
            if sha(path) != descriptor["sha256"]:
                raise ValueError("VERSION_EVIDENCE_HASH_DIFFERS")
            value = read(path)
            if not isinstance(value.get("documents"), list) or sha(path) != descriptor["sha256"]:
                raise ValueError("VERSION_EVIDENCE_INVALID_OR_CHANGED")
            proofs.append((descriptor, value))
        except (KeyError, ValueError, OSError, TypeError) as error:
            errors.append({"path": descriptor.get("path"), "reason": str(error)})
    return proofs, errors


def matching_version_evidence(proofs, mapped, current, parent_key):
    """Reuse earlier page comparisons only when both current byte hashes bind.

    This preserves two distinct files/provenances and does not authorize a
    historical lineage assertion, replacement, or an unreviewed OCR claim.
    """
    for descriptor, proof in proofs:
        for document in proof["documents"]:
            if (document.get("document_id") != mapped["document_id"]
                    or document.get("candidate_pdf_sha256") != mapped["source_pdf_sha256"]):
                continue
            for comparison in document.get("comparisons", []):
                if (comparison.get("parent_key") != parent_key
                        or comparison.get("attachment_key") != current.get("attachment_key")
                        or comparison.get("attachment_sha256") != current["sha256"]):
                    continue
                verified = proof.get("source_file_hashes_verified_before_after", {})
                if (verified.get(document.get("candidate_pdf")) != mapped["source_pdf_sha256"]
                        or verified.get(comparison.get("attachment_path")) != current["sha256"]
                        or sha(Path(current["path"])) != current["sha256"]
                        or sha(Path(mapped["source_pdf"])) != mapped["source_pdf_sha256"]):
                    continue
                equality = comparison.get("all_pages_equal_by_field", {})
                fields = ("text_sha256", "content_stream_sha256", "resources_sha256", "geometry")
                corresponding = comparison.get("page_count_equal") is True and all(equality.get(k) is True for k in fields)
                differs = comparison.get("page_count_equal") is False or any(equality.get(k) is False for k in fields)
                return {"status": "representations_correspond" if corresponding else
                    "representations_differ" if differs else "comparison_incomplete",
                    "evidence": descriptor, "source_sha256": mapped["source_pdf_sha256"],
                    "current_attachment_sha256": current["sha256"], "attachment_key": current["attachment_key"],
                    "all_pages_equal_by_field": equality,
                    "same_bytes": False, "live_parent_identity_required": True,
                    "historical_lineage_proven": False, "automatic_source_replacement_allowed": False}
    return None


def local_audit(scope, config, *, prepared_root, paper_root, live_snapshot=None):
    papers = validate_scope(scope, config)
    inventory = prepared_inventory(prepared_root)
    for extra_root in scope.get("additional_prepared_roots", []):
        if Path(extra_root).resolve() != Path(prepared_root).resolve():
            inventory.extend(prepared_inventory(extra_root))
    mappings, mapping_errors = local_mappings(config)
    version_proofs, version_errors = load_version_evidence(scope)
    bundles = []
    bundle_errors = []
    for path in Path(paper_root).rglob("bundle.json"):
        try:
            manifest = read_bundle(path.parent)
        except (ValueError, OSError) as error:
            bundle_errors.append({"path": str(path), "reason": str(error)})
            continue
        if manifest and manifest["paper"].get("doi", "").lower() in {p["doi"].lower() for p in papers}:
            bundles.append((path, manifest))
    report = {"scope_sha256": scope_digest(scope), "collection_key": scope.get("collection_key"),
        "created_at": datetime.now(timezone.utc).isoformat(), "mode": "offline",
        "external_calls": 0, "business_writes": 0, "generation_calls": 0,
        "mapping_errors": mapping_errors, "bundle_errors": bundle_errors,
        "version_evidence_errors": version_errors,
        "prepared_inventory_errors": [{"manifest": str(p), "reason": m["inventory_error"]}
                                      for p, m in inventory if "inventory_error" in m],
        "papers": [], "complete": False}
    for paper in papers:
        row = {"key": paper["key"], "doi": paper["doi"], "title": paper["title"],
               "identity": "not_checked", "body": [], "prepared": [], "mappings": [],
               "supplements": {"status": "not_checked", "files": []},
               "default_retrieval": "not_checked", "actions": [], "resume": []}
        current = (live_snapshot or {}).get("papers", {}).get(paper["key"])
        candidates = current["pdf_candidates"] if current else paper.get("local_candidates", [])
        row["identity"] = "verified" if current else "historical_hints_only"
        row["body_basis"] = "current_zotero_children" if current else "historical_path_hints"
        for candidate in candidates:
            try:
                inspected = pdf_check(candidate["path"])
                if candidate.get("md5") and inspected["md5"] != candidate["md5"]:
                    raise ValueError("ZOTERO_MD5_DIFFERS")
                row["body"].append({**candidate, **inspected})
            except (OSError, ValueError, RuntimeError, PyPdfError) as error:
                row["body"].append({**candidate, "readable": False, "error": str(error)})
        hashes = {x["sha256"] for x in row["body"] if x.get("readable")}
        associated = [(p, m) for p, m in bundles if m["paper"].get("doi", "").lower() == paper["doi"].lower()]
        row["bundles"] = [str(p) for p, _ in associated]
        for path, manifest in associated:
            status = manifest.get("supplements", {}).get("status", "not_checked")
            row["supplements"].setdefault("bundle_statuses", []).append({"path": str(path), "status": status})
            for material in manifest["files"]:
                if material["role"] == "main_pdf":
                    continue
                item = dict(material)
                try:
                    item["hash_verified"] = sha(path.parent / material["relative_path"]) == material["sha256"]
                except (ValueError, OSError):
                    item["hash_verified"] = False
                row["supplements"]["files"].append(item)
            # A download flag is not proof that all official sources were checked.
            discovery = manifest.get("supplements", {}).get("official_check", {})
            if discovery.get("doi", "").lower() == paper["doi"].lower() and discovery.get("checked_at") and discovery.get("source_url"):
                row["supplements"]["status"] = status
            # Material candidates retain the canonical preparation unchanged.
            # Verify the narrowly allowed provenance overlay before accepting
            # their different JSONL hash as the indexed candidate.
            for material in manifest.get("processing", {}).get("materials", []):
                if not material.get("chunks_path") or not material.get("chunks_sha256"):
                    continue
                candidate = Path(material["chunks_path"])
                try:
                    if candidate.name != "material-chunks.jsonl" or sha(candidate) != material["chunks_sha256"]:
                        continue
                    base_path = candidate.with_name("manifest.json")
                    base = verify_prepared(base_path, read(base_path))
                    derived = load_bundle(candidate)
                    source = next(f for f in manifest["files"] if f["relative_path"] == material["relative_path"])
                    if sha(path.parent / source["relative_path"]) != source["sha256"]:
                        raise ValueError("MATERIAL_SOURCE_DIFFERS")
                    stripped = []
                    for original, overlay in zip(base["rows"], derived["rows"]):
                        modified = json.loads(json.dumps(overlay))
                        allowed = ("doi", "source_role") if source["role"] == "main_pdf" else (
                            "doi", "source_role", "parent_document_id", "attachment_name", "zotero_parent_key", "zotero_attachment_key", "source_url")
                        for field in allowed:
                            if field in original["provenance"]:
                                modified["provenance"][field] = original["provenance"][field]
                            else:
                                modified["provenance"].pop(field, None)
                        if overlay["provenance"].get("doi", "").lower() != paper["doi"].lower():
                            raise ValueError("MATERIAL_DOI_DIFFERS")
                        stripped.append(modified)
                    if stripped != base["rows"] or len(derived["rows"]) != len(base["rows"]):
                        raise ValueError("MATERIAL_PREPARATION_OVERLAY_DIFFERS")
                    row["prepared"].append({"manifest": str(base_path), "source_pdf_sha256": source["sha256"],
                        "current_pdf_hash_matches": source["sha256"] in hashes, "valid": True,
                        "chunks": len(derived["rows"]), "chunks_path": str(candidate), "material_role": source["role"]})
                except (OSError, ValueError, KeyError, StopIteration, ImportError_):
                    continue
        relevant_maps = []
        for mapping in mappings:
            entry = next(iter(mapping["entries"].values()))["record"]
            registry = config.get("source_registry", {}).get(mapping["document_id"], {})
            doi = entry["provenance"].get("doi") or registry.get("doi", "")
            if doi.lower() != paper["doi"].lower():
                continue
            relevant_maps.append(mapping)
            source_hash = entry["provenance"].get("source_pdf_sha256")
            row["mappings"].append({"document_id": mapping["document_id"],
                "dataset_id": mapping["source"]["dataset_id"], "chunks": len(mapping["entries"]),
                "source_pdf_sha256": source_hash, "source_pdf": entry["provenance"].get("source_pdf"),
                "current_pdf_hash_matches": source_hash in hashes, "remote_readback": "not_checked",
                "default_registered": bool(registry and registry.get("source_pdf_sha256") == source_hash
                                           and registry.get("doi", "").lower() == paper["doi"].lower()),
                "source_role": entry["provenance"].get("source_role", "main_pdf")})
        related_hashes = hashes | {m["source_pdf_sha256"] for m in row["mappings"]}
        document_ids = {h[:20] for h in hashes} | {m["document_id"] for m in row["mappings"]}
        for path, manifest in inventory:
            if manifest.get("source_pdf_sha256") not in related_hashes and manifest.get("document_id_hint") not in document_ids:
                continue
            item = {"manifest": str(path), "source_pdf_sha256": manifest.get("source_pdf_sha256"),
                    "current_pdf_hash_matches": manifest.get("source_pdf_sha256") in hashes}
            if "inventory_error" in manifest:
                item.update(valid=False, reason=manifest["inventory_error"])
                row["prepared"].append(item)
                continue
            try:
                chunk_bundle = verify_prepared(path, manifest)
                item.update(valid=True, chunks=len(chunk_bundle["rows"]), chunks_path=str(path.parent / "chunks.jsonl"))
            except (ValueError, KeyError, OSError, ImportError_) as error:
                item.update(valid=False, reason=str(error))
            row["prepared"].append(item)
        for mapping, mapped in zip(relevant_maps, row["mappings"]):
            expected = {k: v["record"] for k, v in mapping["entries"].items()}
            matches = []
            for candidate in row["prepared"]:
                if not candidate.get("valid") or candidate["source_pdf_sha256"] != mapped["source_pdf_sha256"]:
                    continue
                chunk_rows = load_bundle(Path(candidate["chunks_path"]))["rows"]
                if ({r["chunk_id"]: r for r in chunk_rows}
                        == {r["chunk_id"]: r for r in expected.values()}):
                    matches.append(candidate["chunks_path"])
            mapped["prepared_exact_matches"] = matches
            mapped["local_source_mapping_verified"] = bool(matches)
            if not mapped["current_pdf_hash_matches"]:
                for current_body in row["body"]:
                    if not current_body.get("readable"):
                        continue
                    evidence = matching_version_evidence(version_proofs, mapped, current_body, paper["key"])
                    if evidence is not None:
                        mapped["version_evidence"] = evidence
                        break
        if not current:
            row["resume"].append("live_identity")
        if not hashes:
            row["resume"].append("resolve_current_body")
        if len(hashes) > 1:
            row["resume"].append("resolve_body_role_or_version")
        if not any(p.get("valid") and p["current_pdf_hash_matches"] for p in row["prepared"]):
            row["resume"].append("version_review" if row["prepared"] or row["mappings"] else "prepare")
        if any(m["current_pdf_hash_matches"] is False for m in row["mappings"]):
            row["actions"].append("版本差异：禁止直接复用或覆盖旧来源")
        if row["supplements"]["status"] == "not_checked":
            row["resume"].append("official_supplement_check")
        row["resume"].extend(["remote_readback", "formal_default_retrieval"])
        report["papers"].append(row)
    report["summary"] = {"papers": len(report["papers"]), "processed": 0, "verified_complete_skipped": 0,
        "unfinished": len(report["papers"]),
        "local_readable_bodies": sum(any(x.get("readable") for x in p["body"]) for p in report["papers"]),
        "local_mapping_papers": sum(bool(p["mappings"]) for p in report["papers"]),
        "current_hash_prepared_reusable": sum(any(x.get("valid") and x["current_pdf_hash_matches"]
                                                   for x in p["prepared"]) for p in report["papers"]),
        "source_version_differences": sum(bool(p["actions"]) for p in report["papers"]),
        "local_prepared_mapping_aligned": sum(bool(p["mappings"]) and all(m["local_source_mapping_verified"]
                                                   for m in p["mappings"]) for p in report["papers"]),
        "hash_bound_representation_correspondences": sum(any(m.get("version_evidence", {}).get("status") == "representations_correspond"
                                                               for m in p["mappings"]) for p in report["papers"]),
        "hash_bound_representation_differences": sum(any(m.get("version_evidence", {}).get("status") == "representations_differ"
                                                        for m in p["mappings"]) for p in report["papers"])}
    return report


def known_supplement_roles(scope, paper_root):
    """Role evidence requires a current file hash and a FILED attachment receipt."""
    roles = {}
    parents = {p["key"]: p["doi"].lower() for p in scope["papers"]}
    for path in Path(paper_root).glob("**/bundle.json"):
        try:
            manifest = read_bundle(path.parent)
            parent = manifest.get("zotero", {}).get("parent_key")
            if parent not in parents or manifest["paper"].get("doi", "").lower() != parents[parent]:
                continue
            for receipt in manifest.get("zotero", {}).get("supplements", {}).get("files", []):
                if receipt.get("status") != "FILED":
                    continue
                matches = [m for m in manifest["files"] if m["role"] == "supplement" and m["relative_path"] == receipt["relative_path"] and m["sha256"] == receipt["sha256"]]
                if len(matches) != 1:
                    continue
                identity = file_identity(path.parent / receipt["relative_path"])
                if identity["sha256"] == receipt["sha256"]:
                    roles[receipt["attachment_key"]] = {"parent": parent, **identity}
        except (OSError, ValueError, KeyError):
            continue
    return roles


def live_identity(scope, client, *, attachment_roles=None):
    """Read exact parents/children; collection drift is reported, never expands scope."""
    selected = {p["key"] for p in scope["papers"]}
    collection = scope.get("collection_key")
    versions, observed = {}, {}
    def get(path):
        status, raw, headers = client.request("GET", path)
        if status != 200:
            raise ValueError("ZOTERO_READ_FAILED")
        version = next((v for k, v in headers.items() if k.lower() == "last-modified-version"), None)
        if version is None:
            raise ValueError("ZOTERO_SNAPSHOT_VERSION_MISSING")
        # Single-resource Last-Modified-Version is that resource's version,
        # whereas collection item lists report the library/list version.
        # Compare repeat reads and pages of the same resource, not unrelated
        # resource versions (real local collection version can be zero).
        from urllib.parse import parse_qsl, urlencode
        endpoint, _, query = path.partition("?")
        category = endpoint + "?" + urlencode([(k, v) for k, v in parse_qsl(query) if k != "start"])
        if category in versions and versions[category] != str(version):
            raise ValueError("ZOTERO_LIBRARY_CHANGED_DURING_SCAN")
        versions[category] = str(version)
        value = json.loads(raw)
        digest = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if path in observed and observed[path] != digest:
            raise ValueError("ZOTERO_RESOURCE_CHANGED_DURING_SCAN")
        observed[path] = digest
        return value
    members = []
    if collection:
        get("/users/0/collections/" + collection)
        for start in range(0, 1000, 100):
            page = get(f"/users/0/collections/{collection}/items/top?limit=100&start={start}")
            members.extend(x["key"] for x in page)
            if len(page) < 100:
                break
        else:
            raise ValueError("COLLECTION_SCAN_LIMIT")
    snapshot = {"papers": {}, "extra_members_ignored": sorted(set(members) - selected),
                "missing_members": sorted(selected - set(members)) if collection else []}
    for paper in scope["papers"]:
        parent = get("/users/0/items/" + paper["key"])
        data = parent["data"]
        if (parent.get("key") != paper["key"]
                or data.get("DOI", "").lower().strip() != paper["doi"].lower()
                or data.get("itemType") in ("attachment", "note", "annotation")
                or data.get("parentItem") or (collection and paper["key"] not in members)):
            raise ValueError("ZOTERO_SCOPE_IDENTITY_DIFFERS:" + paper["key"])
        children = []
        for start in range(0, 200, 100):
            page = get(f"/users/0/items/{paper['key']}/children?limit=100&start={start}")
            children.extend(page)
            if len(page) < 100:
                break
        else:
            raise ValueError("CHILD_SCAN_LIMIT")
        pdfs = []
        for child in children:
            cd = child["data"]
            if cd.get("parentItem") != paper["key"]:
                raise ValueError("ATTACHMENT_PARENT_DIFFERS")
            if cd.get("contentType") != "application/pdf":
                continue
            uri = urlsplit(child.get("links", {}).get("enclosure", {}).get("href", ""))
            if uri.scheme != "file" or uri.netloc or uri.query or uri.fragment:
                continue
            path = Path(unquote(uri.path).lstrip("/"))
            if not path.resolve().is_relative_to(Path(scope["storage_root"]).resolve()):
                raise ValueError("ATTACHMENT_OUTSIDE_STORAGE")
            role = (attachment_roles or {}).get(child["key"])
            if role is not None:
                identity = file_identity(path)
                if role["parent"] != paper["key"] or identity != {k: role[k] for k in ("sha256", "md5", "size_bytes")} or cd.get("md5") != identity["md5"]:
                    raise ValueError("SUPPLEMENT_ROLE_BINDING_DIFFERS")
                continue
            pdfs.append({"attachment_key": child["key"], "path": str(path), "md5": cd.get("md5")})
        snapshot["papers"][paper["key"]] = {"parent": parent, "children": children, "pdf_candidates": pdfs}
    # Re-read a previously observed collection/items endpoint to detect late drift.
    for paper in scope["papers"]:
        get("/users/0/items/" + paper["key"])
    if collection:
        get(f"/users/0/collections/{collection}/items/top?limit=100&start=0")
    else:
        for paper in scope["papers"]:
            get(f"/users/0/items/{paper['key']}/children?limit=100&start=0")
    snapshot["resource_versions"] = versions
    snapshot["version"] = versions.get(f"/users/0/collections/{collection}/items/top?limit=100", "per-resource")
    return snapshot


def render_report(report):
    if report.get("mode") == "live-processing":
        return render_processing_report(report)
    live = report.get("mode") == "live-audit"
    description = ("当前为获准只读核查；实时身份、远端回读和默认召回分别记录，未执行材料补齐。"
                   if live else "当前为本地核查；历史身份线索、登记、远端回读和真实召回分开记录。")
    lines = ["# 指定 Zotero 范围缺项检查", "", description, "",
        "| 文献／DOI | 正文 | 补件检查与取得 | 解析 | 索引回读 | 默认检索 | 本次动作 | 未完成原因／续办位置 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in report["papers"]:
        readable = [x for x in row["body"] if x.get("readable")]
        body = ("本地候选可读；实时附件关联通过" if row["identity"] == "verified"
                else "本地可读；关联待实时核验") if readable else "当前本地路径未确认；待API解析"
        prepared = [x for x in row["prepared"] if x.get("valid")]
        parse = "未找到有效准备结果"
        if prepared:
            parse = "本地完整性通过" + ("；当前PDF哈希匹配" if any(x["current_pdf_hash_matches"] for x in prepared) else "；与当前PDF哈希不同，待版本判断")
        versions = {m.get("version_evidence", {}).get("status") for m in row["mappings"]}
        if "representations_correspond" in versions:
            parse += "；既有逐页对应证据两端哈希复核通过（原件仍不同）"
        if "representations_differ" in versions:
            parse += "；既有逐页差异证据两端哈希复核通过，不自动复用"
        mapped = sum(m["chunks"] for m in row["mappings"])
        index = f"本地映射{mapped}块；远端未检查" if mapped else "无默认库有效映射；远端未检查"
        if row["mappings"] and not all(m["local_source_mapping_verified"] for m in row["mappings"]):
            index += "；与本地候选未完全对齐"
        statuses = [m["remote_readback"] for m in row["mappings"] if isinstance(m["remote_readback"], dict)]
        if statuses:
            verified = sum(m["status"] == "verified" for m in statuses)
            index = f"本地映射{mapped}块；远端核验{verified}/{len(row['mappings'])}来源"
            issues = sorted({m.get("reason", "") for m in statuses if m["status"] != "verified"})
            if issues:
                index += "；" + "、".join(issues)
        registered = any(m["default_registered"] for m in row["mappings"])
        supplement = "未检查官方来源" if row["supplements"]["status"] == "not_checked" else row["supplements"]["status"]
        supplement += f"；本地记录{len(row['supplements']['files'])}件"
        retrieval = "已登记；当前召回未检查" if registered else "未登记；当前召回未检查"
        if row["default_retrieval"] == "verified":
            retrieval = "正式默认召回及DOI/原文/定位已核验"
        elif any(m.get("formal_retrieval", {}).get("status") == "not_recalled" for m in row["mappings"]):
            retrieval = "正式默认查询未召回所需来源"
        values = [f"{row['title']}<br>{row['key']}<br>{row['doi']}", body, supplement, parse, index,
            retrieval, ("获准只读核查" if live else "本地扫描")
            + ("；版本差异已标记" if row["actions"] else ""), "、".join(row["resume"])]
        lines.append("| " + " | ".join(str(x).replace("|", "\\|").replace("\n", " ") for x in values) + " |")
    counts = ("本次实际派发请求" + str(report.get("external_calls", 0)) + "；批次预留额度计数："
              + json.dumps(report.get("batch_reserved_counts", {}), ensure_ascii=False)) if live else "外部调用0；Embedding调用0"
    lines.extend(["", "统计：" + json.dumps(report["summary"], ensure_ascii=False), "", counts,
        "下载/解析提交/Zotero写入/索引写入/生成调用均0。当前不能宣称本批完整或真实自动处理流程已验收。", ""])
    if report.get("common_stop"):
        lines.extend(["当前停止原因：" + report["common_stop"], ""])
    return "\n".join(lines)


def render_processing_report(report):
    lines = ["# 指定 Zotero 范围处理报告", "", "功能状态：" + report["functional_status"] + "；本批资料部分完成。仅验收资料及检索，回答生成0次。", "",
        "| 文献／DOI | 正文 | 补件检查与取得 | 解析 | 索引回读 | 默认检索 | 本次动作 | 未完成原因／续办位置 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for paper in report["papers"]:
        observation = paper["supplements"]["official_observation"]
        files = [f for f in paper["supplements"]["files"] if f.get("hash_verified")]
        found = len(observation.get("discovered", []))
        status = {"checked_found": "已检查，发现附件", "checked_no_files_in_source": "已检查，PMC未列附件（仅限该来源）",
            "checked_unavailable": "PMC无法检查附件", "check_failed": "检查失败", "checked_not_found": "官方记录未找到"}.get(observation["status"], "未检查")
        supplement = f"{status}；发现{found}、取得并核验{len(files)}件"
        if observation.get("publisher_check"):
            supplement += "；出版商=" + observation["publisher_check"]["status"]
        versions = {m.get("version_evidence", {}).get("status") for m in paper["mappings"]}
        body = "当前父条目/DOI/附件及原件可读性已核验"
        parse = "正文解析/切块来源已核验"
        if "representations_correspond" in versions:
            parse += "；复用逐页对应旧版本（hash不同，未覆盖）"
        if "representations_differ" in versions:
            body += "；两份当前PDF与旧来源存在版本差异"
            parse += "；旧索引保留，当前版本未自动替换"
        records = paper.get("processing", {}).get("materials", {}).get("materials", [])
        for record in records:
            if record["role"] == "supplement":
                parse += "；" + record["filename"] + "=" + record["status"]
        index = "; ".join(str(m["chunks"]) + "块/" + (m["remote_readback"].get("status", "not_checked") if isinstance(m["remote_readback"], dict) else m["remote_readback"]) for m in paper["mappings"])
        retrieval = "本次正式默认召回，DOI/角色/原文/定位通过" if paper["default_retrieval"] == "verified" else paper.get("default_retrieval_note", "未检查")
        values = [paper["title"] + "<br>" + paper["key"] + "<br>" + paper["doi"], body, supplement, parse, index, retrieval,
            "；".join(paper["actions"]), "；".join(paper["resume"]) + "；processing.json / 对应bundle.processing.materials"]
        lines.append("| " + " | ".join(str(x).replace("|", "\\|").replace("\n", " ") for x in values) + " |")
    lines.extend(["", "统计：" + json.dumps(report["summary"], ensure_ascii=False), "",
        "受控外部派发/配置读取计数：" + str(report["external_calls"]) + "（含3个官方价格搜索/读取逻辑请求、1次本地模型配置读取；提供商内部未观测重试不推测）；分项预留计数（未确认调用不退还）：" + json.dumps(report["batch_reserved_counts"], ensure_ascii=False),
        "", "Embedding费用保守上界：人民币" + report["batch_cost_upper_cny"] + "元；非账单金额。原库未写入，Dify工作流未修改，无回答模型调用。",
        "", "限制：可读性和解析完整性不等于全量OCR、图内数值或复杂表格内容已核实。needs_review补件原件和解析已保存，文本/复杂表格与图像待审核，未发布为可靠检索证据。",
        "", "重复执行：" + json.dumps(report.get("repeat_execution", {"status": "pending"}), ensure_ascii=False), ""])
    return "\n".join(lines)


def save_report(report, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    for name, content in (("audit.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n"),
                          ("report.md", render_report(report))):
        temp = output / (name + ".tmp")
        temp.write_text(content, encoding="utf-8")
        temp.replace(output / name)


def execute_material_stages(args, scope, configuration, authorization, report):
    """Keep the eight-row audit even when an affected service must stop."""
    if report.get("common_stop"):
        return report
    try:
        if args.refresh_identity:
            from zotero_scope_online import AuditBudget, bounded_zotero, atomic_json
            budget = AuditBudget(scope, authorization, args.report_dir / "online-budget.json")
            snapshot = live_identity(scope, bounded_zotero(scope, budget), attachment_roles=known_supplement_roles(scope, args.paper_root))
            atomic_json(args.report_dir / "identity-snapshot.json", snapshot)
        if args.discover_materials or args.process_materials and not (args.report_dir / "official-materials.json").is_file():
            from zotero_scope_materials import discover_official_materials, check_publisher_gaps, inspect_linkinghub_landings
            observations = discover_official_materials(scope, authorization, args.report_dir)
            observations = check_publisher_gaps(scope, authorization, args.report_dir)
            observations = inspect_linkinghub_landings(scope, authorization, args.report_dir)
            for paper in report["papers"]:
                paper["supplements"]["official_observation"] = next(r for r in observations if r["key"] == paper["key"])
        if args.process_materials or args.report_only:
            from zotero_scope_processing import process_materials, processing_report
            if not args.report_only:
                process_materials(scope, authorization, args.report_dir, transport=args.transport_base_url,
                                  local_only=args.local_processing_only)
            if not args.local_processing_only:
                report = processing_report(scope, authorization, args.report_dir, args.configuration, report,
                    transport=args.transport_base_url, verify_retrieval=args.verify_retrieval)
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        report.update(common_stop=str(error), complete=False, functional_status="stopped_inspect_checkpoint")
        for paper in report["papers"]:
            paper["resume"].append("公共阶段停止；检查online-budget.json和processing.json：" + str(error))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--configuration", type=Path, default=DEFAULT_CONFIGURATION)
    parser.add_argument("--prepared-root", type=Path, default=ROOT / "RAGFlow/artifacts/prepared")
    parser.add_argument("--paper-root", type=Path, default=default_path("download_dir", "data/papers"))
    parser.add_argument("--live-audit", action="store_true", help="explicitly authorized identity and complete remote readback")
    parser.add_argument("--authorization", type=Path, help="current approved, scope-bound, expiring batch receipt")
    parser.add_argument("--verify-retrieval", action="store_true", help="also verify formal default recall; requires price/runtime proof")
    parser.add_argument("--discover-materials", action="store_true", help="check official metadata and supplement listings for this scope")
    parser.add_argument("--process-materials", action="store_true", help="continue missing materials through approved existing adapters")
    parser.add_argument("--local-processing-only", action="store_true", help="prepare missing main PDFs first; no download or business write")
    parser.add_argument("--report-only", action="store_true", help="refresh an existing processing report without material dispatches")
    parser.add_argument("--refresh-identity", action="store_true", help="recheck current membership and attachment roles before continuation/report")
    parser.add_argument("--transport-base-url", default="http://127.0.0.1:80/api/v1")
    args = parser.parse_args(argv)
    if (args.verify_retrieval or args.discover_materials or args.process_materials or args.report_only or args.refresh_identity) and not args.live_audit:
        parser.error("online stages require --live-audit")
    if args.live_audit:
        if args.authorization is None:
            parser.error("--live-audit requires current --authorization")
        from zotero_scope_online import run_live_audit
        scope, configuration, authorization = read(args.scope), read(args.configuration), read(args.authorization)
        try:
            validate_scope(scope, configuration)
            validate_authorization(scope, authorization)
        except (ValueError, KeyError, TypeError) as error:
            print(json.dumps({"error": str(error), "online_dispatched": False}, ensure_ascii=False), file=sys.stderr)
            return 2
        try:
            if (args.process_materials or args.report_only or args.discover_materials and not args.verify_retrieval) and (args.report_dir / "audit.json").is_file():
                report = read(args.report_dir / "audit.json")
                if (report.get("scope_sha256") != scope_digest(scope) or report.get("common_stop")
                        or not all(p["identity"] == "verified" for p in report["papers"])):
                    raise ValueError("CURRENT_SCOPE_IDENTITY_AUDIT_REQUIRED")
            else:
                report = run_live_audit(scope, configuration, authorization=authorization, output=args.report_dir,
                    prepared_root=args.prepared_root, paper_root=args.paper_root,
                    transport=args.transport_base_url, verify_retrieval=args.verify_retrieval and not args.process_materials)
        except (ValueError, RuntimeError, OSError) as error:
            print(json.dumps({"error": str(error), "dispatch_status": "inspect_online_budget_before_resuming"},
                             ensure_ascii=False), file=sys.stderr)
            return 2
        report = execute_material_stages(args, scope, configuration, authorization, report)
    else:
        report = local_audit(read(args.scope), read(args.configuration), prepared_root=args.prepared_root, paper_root=args.paper_root)
    save_report(report, args.report_dir)
    print(json.dumps({"report": str(args.report_dir / "report.md"), **report["summary"]}, ensure_ascii=False))
    return 2 if report.get("common_stop") else 0


if __name__ == "__main__":
    raise SystemExit(main())
