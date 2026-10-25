"""One finite acquisition API over the existing downloaders and bundle state.

The gateway chooses a route before sending bytes, never retries a rejected
route through another identity, and represents browser authentication as a
resumable action. It does not extract browser sessions or perform indexing.
"""
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from download_ledger import DownloadLedger
from paper_bundle import ensure_bundle, file_identity, read_bundle, record_main_pdf, write_bundle
from publisher_router import normalize_doi, resolve_dois

_TRACE = ContextVar("literature_acquisition_trace", default=None)
_SUPPLEMENTS = ContextVar("literature_acquisition_supplements", default=True)
_DECISIONS = ContextVar("literature_acquisition_decisions", default=None)
CHANNELS = ("auto", "oa", "publisher", "institution_browser")
ROUTE_ORDER = ("verified_local", "oa", "publisher", "institution_browser", "blocked")


def _decision(channel, status, reason):
    decisions = _DECISIONS.get()
    if decisions is not None:
        decisions.append({"channel": channel, "status": status, "reason_code": reason})


def supplements_requested():
    return _SUPPLEMENTS.get()


def validate_selected_pdf(paper, data):
    """The unified DOI API validates bytes before legacy publication/sidecars."""
    if _TRACE.get() is not None:
        from pdf_article_identity import verify_article_identity
        verify_article_identity(data, doi=paper.get("doi"), title=paper.get("title"))


def read_selected_pdf(response, *, limit=64 * 1024 * 1024):
    """Bound unified PDF reads without an extra byte beyond the batch cap."""
    if _TRACE.get() is None:
        return response.read()
    declared = response.headers.get("Content-Length")
    if declared is not None and (not str(declared).isdigit() or int(declared) > limit):
        raise ValueError("PDF_DOWNLOAD_SIZE_LIMIT")
    chunks, received = [], 0
    while received < limit:
        block = response.read(min(65536, limit - received))
        if not block:
            return b"".join(chunks)
        received += len(block)
        chunks.append(block)
    # Unknown/incorrect Content-Length at the boundary needs review, not an
    # additional read to probe EOF. A declared exact-size body is complete.
    if declared is not None and int(declared) == received:
        return b"".join(chunks)
    raise ValueError("PDF_DOWNLOAD_SIZE_LIMIT")


def _existing_job(paper, root):
    from download_jobs import DownloadJobStore
    from download_ledger import identity_keys
    identities = set(identity_keys(paper))
    matches = [row for row in DownloadJobStore(root).state["jobs"].values()
        if identities.intersection(row.get("identifiers", []))]
    if len(matches) > 1:
        raise ValueError("ACQUISITION_JOB_IDENTITY_AMBIGUOUS")
    if matches:
        paper.update(source=matches[0]["source"], id=matches[0]["source_id"])
        return matches[0]
    return None


def dispatch_selected(channel, role, operation):
    """Shared dispatch/diagnostics for legacy callers; preserve their return API."""
    trace = _TRACE.get()
    event = {"channel": channel, "role": role, "status": "dispatching"}
    if trace is not None:
        trace.append(event)
    try:
        result = operation()
        if isinstance(result, dict):
            event["status"] = result.get("status") or result.get("supplements_status") or "returned"
            code = result.get("publiccode") or result.get("error") or result.get("supplements_error")
            if isinstance(code, str) and re.fullmatch(r"[A-Z0-9_]+", code):
                event["reason_code"] = code
        return result
    except Exception:
        event.update(status="failed", reason_code="ACQUISITION_STAGE_EXCEPTION")
        raise


def capabilities(registry):
    return {"interface": "literature_access.acquire", "version": 1, "auto_route_order": list(ROUTE_ORDER),
        "routes": {"oa": {"implemented": True, "live_scope": "existing per-paper OA receipts"},
            "publisher": {"implemented": True, "publishers": {k: {"license_status": v.get("license_status"),
                "endpoint_verified": v.get("endpoint_verified")} for k, v in registry.items() if k != "oa" and isinstance(v, dict)}},
            "institution_browser": {"implemented": True, "mode": "browser action and verified local-file continuation",
                "login_interaction_may_be_required": True, "session_export": False, "live_download_verified": False}},
        "assets": ["main_pdf", "supplements"], "background_service": False,
        "main_document": {"required_format": "pdf", "xml_role": "auxiliary",
            "xml_satisfies_main_document": False},
        "downstream": "existing SAVED -> FILED -> PREPARED -> IMPORTED; independently executed"}


def _paper(value):
    if not isinstance(value, dict):
        raise ValueError("ACQUISITION_PAPER_REQUIRED")
    paper = deepcopy(value)
    paper["doi"] = normalize_doi(paper.get("doi"))
    title = paper.get("title")
    if not isinstance(title, str) or not 1 <= len(title) <= 2048 or any(ord(c) < 32 for c in title):
        raise ValueError("ACQUISITION_TITLE_REQUIRED")
    if "source" not in paper:
        paper.update(source="doi", id=paper["doi"])
    from download_jobs import job_key
    job_key(paper)
    return paper


def _safe_request(request):
    if not isinstance(request, dict) or request.get("version") != 1 or set(request) - {
            "version", "paper", "channel", "include_supplements", "supplement_records", "local_supplements", "contact_email", "browser_receipt", "public_selection"}:
        raise ValueError("ACQUISITION_REQUEST_INVALID")
    channel = request.get("channel", "auto")
    if channel not in CHANNELS or type(request.get("include_supplements", True)) is not bool:
        raise ValueError("ACQUISITION_POLICY_INVALID")
    if request.get("public_selection") is not None and channel not in ("auto", "oa"):
        raise ValueError("ACQUISITION_PUBLIC_SELECTION_CHANNEL_CONFLICT")
    # No browser session/credential field is part of this public JSON contract.
    def check(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str) or re.search(r"cookie|password|session|authorization|api_key|insttoken|tdm_token", key, re.I):
                    raise ValueError("ACQUISITION_SECRET_FIELD_REJECTED")
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)
    check(request)
    for key in ("supplement_records", "local_supplements"):
        if key in request and (not isinstance(request[key], list) or len(request[key]) > 20):
            raise ValueError("ACQUISITION_SUPPLEMENT_RECORDS_INVALID")
    if request.get("include_supplements", True) is False and (request.get("supplement_records") or request.get("local_supplements")):
        raise ValueError("ACQUISITION_SUPPLEMENT_POLICY_CONFLICT")
    return _paper(request["paper"]), channel


def _browser_action(paper):
    _decision("institution_browser", "awaiting_browser", "BROWSER_DOWNLOAD_NOT_COMPLETED")
    return {"status": "awaiting_browser", "channel": "institution_browser", "role": "main_pdf",
        "doi": paper["doi"], "title": paper["title"], "article_reference": "https://doi.org/" + paper["doi"],
        "action": "Use the official library login in the browser; obtain the exact article and resume with a file receipt.",
        "login_state_claim": "not_assessed", "session_export": False,
        "reason_code": "BROWSER_DOWNLOAD_NOT_COMPLETED"}


def _record_saved_job(paper, root, known):
    """Caller holds the existing global lock; recover rather than replay I/O."""
    from download_jobs import DownloadJobStore
    jobs = DownloadJobStore(root)
    key = jobs.ensure(paper, "unified literature acquisition")
    prepared = {**paper, **known, "status": "exists"}
    ledger = DownloadLedger(root)
    if jobs.prepare(key, prepared, ledger):
        jobs.mark_fetching(key)
        jobs.record_result(key, prepared, ledger)
    if jobs.state["jobs"][key]["status"] != "SAVED":
        raise ValueError("ACQUISITION_JOB_REVIEW_REQUIRED")
    return key


def _browser_main(paper, root, receipt):
    """Validate browser-acquired bytes and reuse standard bundle/ledger/jobs."""
    from publishers.base import GLOBAL_PROCESS_LOCK
    from publisher_router import _safe_public_url
    from process_lock import process_lock
    from paper_supplements import _publish_temp
    from pdf_article_identity import verify_article_identity, MAX_PDF_BYTES
    if (not isinstance(receipt, dict) or set(receipt) - {"doi", "path", "sha256", "source_url", "permission_evidence", "confirmed_browser_download"}
            or receipt.get("confirmed_browser_download") is not True or normalize_doi(receipt.get("doi")) != paper["doi"]
            or _safe_public_url(receipt.get("source_url"))[1] is not None
            or _safe_public_url(receipt.get("permission_evidence"))[1] is not None
            or not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("sha256")))):
        raise ValueError("BROWSER_FILE_RECEIPT_REQUIRED")
    source = Path(receipt["path"])
    identity = file_identity(source)
    if identity["sha256"] != receipt["sha256"] or identity["size_bytes"] > MAX_PDF_BYTES:
        raise ValueError("BROWSER_FILE_HASH_OR_SIZE_DIFFERS")
    verify_article_identity(source.read_bytes(), doi=paper["doi"], title=paper["title"])
    with process_lock(GLOBAL_PROCESS_LOCK):
        from download_jobs import DownloadJobStore, MANUAL_STATES
        jobs = DownloadJobStore(root)
        key = jobs.ensure(paper, "unified institution browser acquisition")
        if jobs.state["jobs"][key]["status"] in MANUAL_STATES:
            raise ValueError("BROWSER_JOB_REVIEW_REQUIRED")
        folder = ensure_bundle(root, paper)
        destination = folder / ("doi-" + hashlib.sha256(paper["doi"].encode()).hexdigest()[:24] + ".pdf")
        temporary = None
        try:
            if destination.exists():
                if file_identity(destination) != identity:
                    raise ValueError("BROWSER_FILE_DESTINATION_CONFLICT")
            else:
                descriptor, name = tempfile.mkstemp(prefix=".browser-", suffix=".partial", dir=folder)
                temporary = Path(name)
                with os.fdopen(descriptor, "wb") as stream, source.open("rb") as incoming:
                    for block in iter(lambda: incoming.read(1024 * 1024), b""):
                        stream.write(block)
                    stream.flush();os.fsync(stream.fileno())
                if file_identity(temporary) != identity or file_identity(source) != identity:
                    raise ValueError("BROWSER_FILE_CHANGED")
                _publish_temp(temporary, destination);temporary = None
            ledger = DownloadLedger(root)
            known = ledger.record(paper, destination)
            record_main_pdf(root, paper, destination, source="institution_browser", license_value="browser-confirmed-file-specific-access")
            manifest = read_bundle(folder)
            manifest["browser_acquisition"] = {"doi": paper["doi"], "sha256": identity["sha256"],
                "source_url": receipt["source_url"], "permission_evidence": receipt["permission_evidence"],
                "browser_download_confirmed": True, "session_export": False}
            write_bundle(folder, manifest)
            key = _record_saved_job(paper, root, known)
            return {**known, "status": "exists", "source": "institution_browser", "job_key": key}
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _route_main(paper, root, channel, registry, *, governor, metadata_client, confirm_publisher, browser_receipt,
                preserve_job_identity=False, public_selection=None):
    if channel == "institution_browser":
        if browser_receipt:
            _decision("institution_browser", "selected", "BROWSER_FILE_RECEIPT")
        return dispatch_selected("institution_browser", "main_pdf", lambda:
            _browser_main(paper, root, browser_receipt) if browser_receipt else _browser_action(paper))
    if public_selection is not None:
        from publisher_router import _safe_public_url
        if (not isinstance(public_selection, dict) or set(public_selection) != {
                "provider", "candidate_id", "permission_evidence", "confirmed"}
                or public_selection["provider"] not in ("openalex", "europepmc", "crossref")
                or not re.fullmatch(r"[0-9a-f]{64}", str(public_selection["candidate_id"]))
                or public_selection["confirmed"] is not True
                or _safe_public_url(public_selection["permission_evidence"])[1] is not None):
            raise ValueError("ACQUISITION_PUBLIC_SELECTION_INVALID")
        from fetch_fulltext_by_doi import discover
        from fulltext_download import PublicFulltextAdapter, candidate_id, validate_selection, make_download_plan
        candidates = discover(paper["doi"], metadata_client, (public_selection["provider"],))
        matches = [c for c in candidates if candidate_id(c) == public_selection["candidate_id"]]
        if len(matches) != 1:
            raise ValueError("FULLTEXT_CANDIDATE_CHANGED_OR_ABSENT")
        selected = validate_selection(matches[0], doi=paper["doi"], confirmed=True,
            permission_evidence=public_selection["permission_evidence"])
        _decision("oa", "selected", "EXPLICIT_PUBLIC_CANDIDATE")
        return PublicFulltextAdapter(selected, confirmed=True,
            permission_evidence=public_selection["permission_evidence"]).fetch_pdf(
                make_download_plan(selected), root, governor, paper=paper,
                expected_title=paper["title"], noncommercial_research=True)
    from fulltext_url import europepmc_pdf_api_url
    from paper_downloader import PaperDownloader
    from publisher_router import _call_metadata_source
    # A pinned publisher request does not quietly download through an OA URL.
    if channel == "publisher":
        local = resolve_dois([paper["doi"]], registry)[0]
        selected = _publisher_main(paper, root, local, registry, governor, confirm_publisher)
        return {**selected, "status": "blocked"} if selected.get("status") == "unavailable" else selected
    # Exact core metadata selects the established OA PDF API, before any
    # download attempt. A denial is terminal, never a trigger for browser use.
    core, core_error = _call_metadata_source(metadata_client, "get_europepmc", paper["doi"], False)
    if core_error:
        _decision("oa", "stopped", core_error)
        return {"status": "blocked", "error": core_error}
    if core is not None:
        records = [r for r in core.get("resultList", {}).get("result", []) if r.get("doi", "").lower() == paper["doi"]]
        if len(records) > 1:
            raise ValueError("ACQUISITION_METADATA_IDENTITY_AMBIGUOUS")
        if records:
            record = records[0]
            url = europepmc_pdf_api_url(record)
            if url and channel in ("auto", "oa"):
                # Use the freshly matched public PMID for the existing PMC
                # supplement/filing contract, retaining a pre-existing job key.
                if (not preserve_job_identity and paper["source"] == "doi" and record.get("source") == "MED"
                        and re.fullmatch(r"[1-9][0-9]{0,15}", str(record.get("id", "")))):
                    paper.update(source="pubmed", id=str(record["id"]))
                candidate = {**paper, "pdf_url": url, "pmcid": record["pmcid"], "open_access": True,
                    "supplements_oa_eligible": record.get("isOpenAccess") == "Y", "supplements_license": record.get("license")}
                _decision("oa", "selected", "EXACT_EUROPEPMC_PDF")
                return PaperDownloader(governor=governor).download_pdf(candidate, root)
    # Resolve public sources before checking *any* publisher credential.
    plans = resolve_dois([paper["doi"]], registry, preview=False, metadata_client=metadata_client,
        credential_present=None,
        noncommercial_research=True, resolve_metadata=True)
    plan = plans[0]
    if plan.action == "download" and plan.adapter == "oa":
        _decision("oa", "selected", plan.reason)
        from publishers import create_adapter
        return create_adapter("oa", registry["oa"]).fetch_pdf(plan, root, governor, paper=paper,
            noncommercial_research=True, expected_title=paper["title"])
    # Only an explicit absence/local eligibility decision can advance. Never
    # turn metadata refusal, unsafe URL, uncertain rights or bad bytes into a
    # retry through another identity. No second metadata lookup is needed.
    eligible_reasons = {"publisher_not_approved", "permission_evidence_missing", "publisher_endpoint_unverified",
        "springer_pdf_endpoint_unverified", "credential_presence_unavailable", "publisher_unrecognized",
        "metadata_unavailable"}
    if plan.action != "download" and plan.reason not in eligible_reasons:
        _decision("oa", "stopped", plan.reason)
        return {"status": "manual", "error": plan.reason}
    _decision("oa", "unavailable", "NO_EXECUTABLE_OA_SOURCE")
    if channel == "oa":
        return {"status": "blocked", "error": "NO_EXECUTABLE_OA_SOURCE"}
    publisher = _publisher_main(paper, root, plan, registry, governor, confirm_publisher)
    if publisher.get("status") != "unavailable":
        return publisher
    if browser_receipt:
        _decision("institution_browser", "selected", "BROWSER_FILE_RECEIPT")
        return dispatch_selected("institution_browser", "main_pdf", lambda: _browser_main(paper, root, browser_receipt))
    return {**_browser_action(paper), "preceding_route_decision": publisher["error"]}


def _publisher_main(paper, root, plan, registry, governor, confirmed):
    from publisher_router import _subscription_plan
    entry = registry.get(getattr(plan, "publisher", None))
    if not isinstance(entry, dict):
        reason = "publisher_unrecognized"
    else:
        local = _subscription_plan(paper["doi"], plan.publisher, getattr(plan, "doi_prefix", None), entry,
            preview=True, credential_present=None, oa_status=getattr(plan, "oa_status", "unknown"),
            noncommercial_research=True, title=paper["title"])
        reason = local.reason
        if reason == "credential_presence_not_checked_in_preview":
            domains = getattr(governor, "state", {}).get("domains", {})
            if any(domains.get(host, {}).get("paused_reason") for host in entry.get("api_domains", [])):
                reason = "PUBLISHER_DOMAIN_PAUSED"
            elif not confirmed:
                reason = "PUBLISHER_CURRENT_BATCH_NOT_CONFIRMED"
            else:
                plan = _subscription_plan(paper["doi"], plan.publisher, getattr(plan, "doi_prefix", None), entry,
                    preview=False, credential_present=lambda name: bool(os.environ.get(name)),
                    oa_status=getattr(plan, "oa_status", "unknown"), noncommercial_research=True,
                    title=paper["title"])
                reason = plan.reason
                if plan.action == "download":
                    _decision("publisher", "selected", reason)
                    from publishers import create_adapter
                    return create_adapter(plan.adapter, entry).fetch_pdf(plan, root, governor, paper=paper,
                        noncommercial_research=True, expected_title=paper["title"])
    unavailable = reason in {"publisher_unrecognized", "PUBLISHER_CURRENT_BATCH_NOT_CONFIRMED",
        "publisher_not_approved", "permission_evidence_missing", "publisher_endpoint_unverified",
        "springer_pdf_endpoint_unverified", "credential_presence_unavailable", "credential_missing"}
    _decision("publisher", "unavailable" if unavailable else "stopped", reason)
    return {"status": "unavailable" if unavailable else "blocked", "error": reason}


def acquire(request, download_dir, *, execute=False, confirm_download=False, confirm_publisher=False,
            noncommercial_research=False, registry=None, governor=None, metadata_client=None):
    """Single project API for a paper's main PDF and requested supplements.

    Preview is local-only. Browser acquisition is two-phase through this same
    API: awaiting_browser, then a content-bound local receipt after login.
    """
    paper, channel = _safe_request(request)
    if request.get("supplement_records"):
        paper["supplements"] = deepcopy(request["supplement_records"])
    root = Path(download_dir).absolute()
    if registry is None:
        registry = json.loads(Path(__file__).with_name("publisher_registry.json").read_text(encoding="utf-8"))
    result = {"version": 1, "doi": paper["doi"], "title": paper["title"], "mode": "execute" if execute else "preview",
        "status": "preview", "requested_channel": channel, "route_order": list(ROUTE_ORDER),
        "route_decisions": [], "stages": {}, "attempts": [],
        "zotero": "not_run", "parsing": "not_run", "ragflow": "not_run"}
    known = DownloadLedger(root).lookup(paper)
    job = _existing_job(paper, root)
    if known:
        from pdf_article_identity import verify_article_identity
        verify_article_identity(Path(known["pdf_path"]).read_bytes(), doi=paper["doi"], title=paper["title"])
    if not execute:
        result["stages"]["main_pdf"] = {"status": "reusable" if known else "route_required", "local": known}
        result["stages"]["supplements"] = {"status": "not_checked"}
        result["existing_job_status"] = job.get("status") if job else None
        result.update(network_requests=0, credential_reads=0, writes=False)
        return result
    if confirm_download is not True or noncommercial_research is not True:
        raise ValueError("ACQUISITION_CURRENT_BATCH_CONFIRMATION_REQUIRED")
    if job:
        from download_jobs import MANUAL_STATES, DEFERRED_STATES
        if job["status"] in MANUAL_STATES | DEFERRED_STATES or not known and job["status"] in {"FETCHING", "VERIFIED", "SAVED"}:
            return {**result, "status": "needs_review", "existing_job_status": job["status"],
                "reason_code": "ACQUISITION_EXISTING_JOB_REQUIRES_RESUME", "network_requests": 0}
    if channel == "publisher" and not known:
        from publisher_router import _subscription_plan
        local = resolve_dois([paper["doi"]], registry)[0]
        entry = registry.get(local.publisher)
        if not isinstance(entry, dict):
            reason = "publisher_unrecognized"
        else:
            local = _subscription_plan(paper["doi"], local.publisher, local.doi_prefix, entry,
                preview=True, credential_present=None, oa_status="unknown", noncommercial_research=True)
            reason = local.reason
        if reason != "credential_presence_not_checked_in_preview":
            return {**result, "status": "blocked", "reason_code": reason,
                "route_decisions": [{"channel": "verified_local", "status": "unavailable", "reason_code": "NO_VERIFIED_LOCAL_PDF"},
                    {"channel": "publisher", "status": "unavailable", "reason_code": reason}],
                "stages": {"main_pdf": {"status": "blocked", "error": reason},
                    "supplements": {"status": "not_requested"}},
                "network_requests": 0, "credential_reads": 0}
    if governor is None:
        from download_guard import RequestGovernor
        governor = RequestGovernor(Path(__file__).with_name("download_state.json"), interval_seconds=10, max_requests_per_minute=6)
    token = _TRACE.set(result["attempts"])
    supplement_token = _SUPPLEMENTS.set(request.get("include_supplements", True))
    decision_token = _DECISIONS.set(result["route_decisions"])
    try:
        if known:
            _decision("verified_local", "selected", "VERIFIED_LEDGER_BYTES_AND_ARTICLE_IDENTITY")
            from pdf_article_identity import verify_article_identity
            verify_article_identity(Path(known["pdf_path"]).read_bytes(), doi=paper["doi"], title=paper["title"])
            receipt = request.get("browser_receipt")
            if receipt and (normalize_doi(receipt.get("doi")) != paper["doi"] or receipt.get("sha256") != known["sha256"]):
                raise ValueError("BROWSER_RECEIPT_DIFFERS_FROM_SAVED_PDF")
            main = {**known, "status": "exists", "source": "verified_local"}
        else:
            _decision("verified_local", "unavailable", "NO_VERIFIED_LOCAL_PDF")
            if metadata_client is None and channel in ("auto", "oa"):
                from publisher_metadata import PublisherMetadataClient
                from publishers.base import reserve_daily_request
                metadata_client = PublisherMetadataClient(governor, email=request.get("contact_email"),
                    budget_reserver=lambda url: reserve_daily_request(root, url, 100), noncommercial_research=True)
            main = _route_main(paper, root, channel, registry, governor=governor, metadata_client=metadata_client,
                confirm_publisher=confirm_publisher, browser_receipt=request.get("browser_receipt"),
                preserve_job_identity=job is not None, public_selection=request.get("public_selection"))
        result["stages"]["main_pdf"] = {k: main[k] for k in ("status", "source", "error", "publiccode", "pdf_path", "sha256", "job_key", "action", "article_reference", "login_state_claim", "preceding_route_decision", "reason_code") if k in main}
        if main.get("status") not in ("downloaded", "exists"):
            result["status"] = main.get("status", "blocked")
            reason = main.get("reason_code") or main.get("publiccode") or main.get("error")
            if not isinstance(reason, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,100}", reason):
                reason = "MAIN_PDF_NOT_READY"
            result["blockers"] = [{"stage": "main_pdf", "reason_code": reason,
                "continuation": "review_browser_access_and_resume_with_verified_file" if result["status"] == "awaiting_browser"
                    else "reconcile_permission_or_failure_before_resume"}]
            result["stages"]["supplements"] = {"status": "not_run", "reason_code": "MAIN_PDF_NOT_READY"}
            return result
        pdf = Path(main["pdf_path"])
        from pdf_article_identity import verify_article_identity
        verify_article_identity(pdf.read_bytes(), doi=paper["doi"], title=paper["title"])
        from publishers.base import GLOBAL_PROCESS_LOCK
        from process_lock import process_lock
        with process_lock(GLOBAL_PROCESS_LOCK):
            result["stages"]["main_pdf"]["job_key"] = _record_saved_job(paper, root, DownloadLedger(root).lookup(paper))
        manifest = read_bundle(pdf.parent)
        if request.get("include_supplements", True):
            from paper_supplements import acquire_supplements, register_local_supplement
            material_paper = deepcopy(manifest["paper"]) if manifest else paper
            if request.get("supplement_records"):
                material_paper["supplements"] = request["supplement_records"]
            # Newly downloaded files may already have invoked the common
            # completion hook. Reuse that result instead of immediately retrying.
            local_results = []
            for local in request.get("local_supplements", []):
                if not isinstance(local, dict) or set(local) != {"path", "sha256", "record"}:
                    raise ValueError("ACQUISITION_LOCAL_SUPPLEMENT_INVALID")
                local_results.append(dispatch_selected("institution_browser", "supplement", lambda:
                    register_local_supplement(material_paper, pdf, local["path"], local["record"], local["sha256"])))
            supplement = ({"supplements_status": main["supplements_status"], "supplements": main.get("supplements", [])}
                if "supplements_status" in main and not local_results else acquire_supplements(material_paper, pdf, governor=governor))
            if local_results:
                supplement["local_receipts"] = local_results
            result["stages"]["supplements"] = supplement
        else:
            result["stages"]["supplements"] = {"status": "not_requested"}
        result["status"] = "saved" if (not request.get("include_supplements", True) or
            result["stages"]["supplements"].get("supplements_status") in ("complete", "none")) else "partial"
        manifest = read_bundle(pdf.parent)
        if manifest is not None:
            manifest.setdefault("processing", {})["acquisition"] = {"updated_at": datetime.now(timezone.utc).isoformat(),
                "interface": "literature_access.acquire", "requested_channel": channel, "status": result["status"],
                "attempts": result["attempts"], "route_decisions": result["route_decisions"],
                "main_source_sha256": file_identity(pdf)["sha256"]}
            write_bundle(pdf.parent, manifest)
            result["bundle_path"] = str(pdf.parent / "bundle.json")
        return result
    except Exception as failure:
        # Preserve partial stages and durable legacy checkpoints. No provider
        # retry and no exception text (possibly containing an authenticated URL).
        code = getattr(failure, "code", None) or str(failure)
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,100}", code):
            code = "ACQUISITION_STAGE_FAILED"
        result.update(status="needs_review", reason_code=code)
        return result
    finally:
        _DECISIONS.reset(decision_token)
        _SUPPLEMENTS.reset(supplement_token)
        _TRACE.reset(token)
