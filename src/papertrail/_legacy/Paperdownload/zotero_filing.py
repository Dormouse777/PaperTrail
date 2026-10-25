#!/usr/bin/env python3
"""Preview or file one verified SAVED PubMed, DOI, or arXiv job.

Only --execute requests interactive write authorization. No cloud API, SQLite,
downloads, model calls, secret persistence, destructive repair, or blind retry.
The FILED sidecar is issued only after parent/collection/PDF readback succeeds.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from process_lock import process_lock
from download_ledger import DownloadLedger, LedgerConflict, normalized_doi
from download_jobs import DownloadJobStore
from paper_bundle import BundleError, file_identity as bundle_file_identity, read_bundle, verified_supplements, write_bundle
from zotero_topics import choose_topic
from zotero_preflight import compare_with_zotero, normalize_title
from pdf_article_identity import (ArticleIdentityError, verify_article_identity,
                                  MAX_PDF_BYTES as MAX_IDENTITY_PDF_BYTES)

FILING_STATE = ".paper-zotero-filing.json"
KEY = re.compile(r"[A-Z0-9]{8}\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")
MAX_PDF_BYTES = 256 * 1024 * 1024
MAX_SUPPLEMENT_BYTES = 256 * 1024 * 1024
from workflow_defaults import default_path

DEFAULT_STORAGE = default_path("zotero_storage", "data/zotero-storage")


class FilingError(RuntimeError):
    """Fixed public error codes only: never include API bodies or keys."""


def _key(value):
    if not isinstance(value, str) or not KEY.fullmatch(value):
        raise FilingError("INVALID_OBJECT_KEY")
    return value


def _json(path):
    if path.is_symlink() or not path.is_file():
        raise FilingError("MISSING_OR_UNSAFE_STATE")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError, OSError) as exc:
        raise FilingError("INVALID_LOCAL_JSON") from exc
    if not isinstance(value, dict) or type(value.get("version")) is not int or value.get("version") != 1:
        raise FilingError("UNSUPPORTED_STATE")
    return value


def _file_identity(pdf):
    if pdf.is_symlink() or not pdf.is_file():
        raise FilingError("MISSING_OR_UNSAFE_PDF")
    size = pdf.stat().st_size
    if not 1024 <= size <= MAX_PDF_BYTES:
        raise FilingError("PDF_SIZE_OUT_OF_SCOPE")
    sha, md5 = hashlib.sha256(), hashlib.md5()
    with pdf.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise FilingError("INVALID_PDF_HEADER")
        stream.seek(0)
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
            md5.update(block)
    return {"sha256": sha.hexdigest(), "md5": md5.hexdigest(), "size": size}


def _any_file_identity(path):
    """Hash a stored attachment without imposing the main-PDF signature rule."""
    try:
        result = bundle_file_identity(path)
    except (BundleError, OSError, ValueError) as exc:
        code = str(exc)
        if not re.fullmatch(r"[A-Z0-9_]+", code):
            code = "ATTACHMENT_FILE_UNAVAILABLE"
        raise FilingError(code) from None
    return {"sha256": result["sha256"], "md5": result["md5"], "size": result["size_bytes"]}


def verified_job(directory, job_key):
    raw_root = Path(directory)
    if raw_root.is_symlink() or not raw_root.is_dir():
        raise FilingError("INVALID_DOWNLOAD_DIRECTORY")
    root = raw_root.resolve(strict=True)
    jobs = _json(root / ".paper-download-jobs.json").get("jobs")
    ledger = _json(root / ".paper-download-ledger.json").get("entries")
    if not isinstance(jobs, dict) or not isinstance(ledger, dict):
        raise FilingError("INVALID_DOWNLOAD_STATE")
    job = jobs.get(job_key)
    if not isinstance(job, dict) or job.get("status") != "SAVED":
        raise FilingError("JOB_NOT_SAVED")
    source, sid = job.get("source"), job.get("source_id")
    if not isinstance(sid, str) or not sid:
        raise FilingError("INVALID_PUBLICATION_JOB")
    if source == "pubmed":
        if not re.fullmatch(r"[0-9]+", sid) or job_key != "pubmed:" + sid:
            raise FilingError("INVALID_PUBMED_JOB")
    elif source == "doi":
        doi = normalized_doi(sid)
        if not doi or doi != sid or job_key != "doi:" + doi:
            raise FilingError("INVALID_DOI_JOB")
    elif source == "arxiv":
        if (not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[A-Za-z.-]+/\d{7})(?:v\d+)?", sid)
                or job_key != "arxiv:" + sid.lower()):
            raise FilingError("INVALID_ARXIV_JOB")
        sid = sid.lower()
    else:
        raise FilingError("UNSUPPORTED_PUBLICATION_SOURCE")
    filename = job.get("pdf_filename")
    if (not isinstance(filename, str) or Path(filename).name != filename
            or "/" in filename or "\\" in filename or not filename.lower().endswith(".pdf")):
        raise FilingError("INVALID_PDF_FILENAME")
    matches = [row for row in ledger.values() if isinstance(row, dict)
               and row.get("filename") == filename and row.get("sha256") == job.get("pdf_sha256")]
    if not matches or any(row != matches[0] for row in matches):
        raise FilingError("LEDGER_CONFLICT")
    try:
        ledger_reader = DownloadLedger(root)
        pdf = ledger_reader.source_path(matches[0])
        if not pdf.exists():
            pdf, _, _ = ledger_reader._verify_entry(matches[0])
    except (LedgerConflict, OSError):
        raise FilingError("MISSING_OR_UNSAFE_PDF") from None
    identity = _file_identity(pdf)
    if (identity["sha256"] != job.get("pdf_sha256")
            or identity["size"] != job.get("pdf_size_bytes")):
        raise FilingError("SAVED_PDF_CHANGED")
    identifiers = job.get("identifiers")
    required_id = {"pubmed": "pmid:" + sid,
                   "doi": "doi:" + sid,
                   "arxiv": "arxiv:" + sid}[source]
    if not isinstance(identifiers, list) or required_id not in identifiers:
        raise FilingError("INVALID_PUBLICATION_IDENTIFIERS")
    candidate = {"source": source, "id": sid, "title": job.get("title")}
    for prefix, field in (("doi:", "doi"), ("pmcid:", "pmcid")):
        values = [i[len(prefix):] for i in identifiers if isinstance(i, str) and i.startswith(prefix)]
        if len(values) > 1:
            raise FilingError("CONFLICTING_PUBLICATION_IDENTIFIERS")
        if values:
            candidate[field] = values[0]
    if source == "doi" and candidate.get("doi") != sid:
        raise FilingError("INVALID_PUBLICATION_IDENTIFIERS")
    confirmations = 0
    for identifier in identifiers:
        row = ledger.get(identifier)
        if row is None:
            continue
        if (not isinstance(row, dict) or row.get("filename") != filename
                or row.get("sha256") != identity["sha256"] or row.get("size_bytes") != identity["size"]):
            raise FilingError("LEDGER_CONFLICT")
        confirmations += 1
    if not confirmations:
        raise FilingError("UNCONFIRMED_SAVED_PDF")
    # Canonical publisher files and provenance-bearing imports must not bypass
    # body identity by entering through the generic SAVED queue or resume path.
    # Older PMID-only/manual rows without these markers retain their old gate.
    if filename.startswith('doi-') or (root / filename).with_suffix('.provenance.json').exists() or pdf.with_suffix('.provenance.json').exists():
        if identity['size'] > MAX_IDENTITY_PDF_BYTES:
            raise FilingError('ARTICLE_PDF_UNREADABLE')
        data = pdf.read_bytes()
        if len(data) != identity['size'] or hashlib.sha256(data).hexdigest() != identity['sha256']:
            raise FilingError('SAVED_PDF_CHANGED')
        try:
            verify_article_identity(data, doi=candidate.get('doi'), title=candidate.get('title'))
        except ArticleIdentityError as failure:
            raise FilingError(failure.code) from None
    return root, job, candidate, pdf, identity


def collection_name(query):
    if not isinstance(query, str):
        raise FilingError("MISSING_COLLECTION_QUERY")
    name = re.sub(r"\[[A-Za-z ]+\]", "", query).strip().strip('"')
    name = re.sub(r"[<>:\"/\\|?*]", " ", name)
    name = " ".join(name.split())
    if not name or len(name) > 150 or any(ord(c) < 32 for c in name):
        raise FilingError("INVALID_COLLECTION_NAME")
    return name


def _header(headers, name):
    return next((v for k, v in headers.items() if k.lower() == name.lower()), None)


def _current_library_version(client):
    """Read Zotero's current library version for one optimistic write."""
    status, _body, headers = client.request(
        "GET", "/users/0/items?format=json&limit=1&start=0")
    version = _header(headers, "Last-Modified-Version")
    if status != 200 or not isinstance(version, str) or not version.isdigit():
        raise FilingError("INVALID_SNAPSHOT_VERSION")
    return version


def snapshot(client, kind):
    rows, keys, version = [], set(), None
    began = time.monotonic()
    for start in range(0, 250000, 100):
        if time.monotonic() - began >= 120:
            raise FilingError("SNAPSHOT_TIME_BUDGET")
        status, body, headers = client.request("GET", f"/users/0/{kind}?format=json&limit=100&start={start}")
        current = _header(headers, "Last-Modified-Version")
        if status != 200 or not isinstance(current, str) or not current.isdigit():
            raise FilingError("INVALID_SNAPSHOT_VERSION")
        if version is not None and current != version:
            raise FilingError("LIBRARY_CHANGED_DURING_SCAN")
        version = current
        try:
            page = json.loads(body)
        except (ValueError, UnicodeError) as exc:
            raise FilingError("INVALID_SNAPSHOT_JSON") from exc
        if not isinstance(page, list) or len(page) > 100:
            raise FilingError("INVALID_SNAPSHOT_PAGE")
        for row in page:
            if not isinstance(row, dict) or not isinstance(row.get("data"), dict):
                raise FilingError("INVALID_SNAPSHOT_OBJECT")
            key = _key(row.get("key"))
            if key in keys:
                raise FilingError("DUPLICATE_SNAPSHOT_KEY")
            keys.add(key)
            rows.append(row)
        if len(page) < 100:
            if time.monotonic() - began >= 120:
                raise FilingError("SNAPSHOT_TIME_BUDGET")
            return rows, version
    raise FilingError("SNAPSHOT_TOO_LARGE")


def candidate_snapshot(client, candidate, lookup=None):
    """Refresh bounded matches under the same library version precondition.

    A caller-supplied candidate lookup already implements identifier and title
    queries. Bracket those reads with global library versions so unrelated sync
    changes cannot be hidden, and keep the write's version precondition.
    """
    if lookup is None:
        return snapshot(client, "items")

    def version():
        status, _body, headers = client.request(
            "GET", "/users/0/items?format=json&limit=1&start=0")
        value = _header(headers, "Last-Modified-Version")
        if status != 200 or not isinstance(value, str) or not value.isdigit():
            raise FilingError("INVALID_SNAPSHOT_VERSION")
        return value

    before = version()
    rows = lookup(candidate)
    if not isinstance(rows, list):
        raise FilingError("INVALID_CANDIDATE_LOOKUP")
    after = version()
    if before != after:
        raise FilingError("LIBRARY_CHANGED_DURING_SCAN")
    # Validate all returned identities before they can influence a mutation.
    compare_with_zotero(candidate, rows)
    return rows, after


def _choose_parent(candidate, items):
    match = compare_with_zotero(candidate, items)
    if match["status"] == "clear":
        return None
    if match["status"] != "duplicate" or len(match["matches"]) != 1:
        raise FilingError("AMBIGUOUS_PARENT_MATCH")
    return _key(match["matches"][0]["key"])


def _choose_collection(name, collections):
    found = [row for row in collections if normalize_title(row["data"].get("name")) == normalize_title(name)]
    if len(found) > 1:
        raise FilingError("AMBIGUOUS_COLLECTION_NAME")
    return _key(found[0]["key"]) if found else None


def _source_file(client, child_key, storage_root):
    status, body, _ = client.request("GET", f"/users/0/items/{_key(child_key)}/file/view/url")
    if status != 200:
        raise FilingError("ATTACHMENT_PATH_UNAVAILABLE")
    try:
        uri = urllib.parse.urlsplit(body.decode("utf-8").strip())
    except (ValueError, UnicodeError) as exc:
        raise FilingError("INVALID_ATTACHMENT_PATH") from exc
    if uri.scheme != "file" or uri.netloc or uri.query or uri.fragment:
        raise FilingError("NONLOCAL_ATTACHMENT_PATH")
    raw = urllib.parse.unquote(uri.path)
    if re.match(r"^/[A-Za-z]:/", raw):
        raw = raw[1:]
    path = Path(raw)
    try:
        allowed = Path(storage_root).resolve(strict=True)
        actual = path.resolve(strict=True)
    except OSError as exc:
        raise FilingError("ATTACHMENT_FILE_UNAVAILABLE") from exc
    if path.is_symlink() or not actual.is_relative_to(allowed):
        raise FilingError("ATTACHMENT_OUTSIDE_STORAGE")
    return actual


def attachment_plan(client, parent_key, pdf, identity, storage_root, *,
                    verified_supplements=()):
    if parent_key is None:
        return None, False
    children = client.get_json(f"/users/0/items/{parent_key}/children?format=json")
    if not isinstance(children, list):
        raise FilingError("INVALID_CHILDREN")
    # A verified bundled PDF supplement is also an application/pdf child of
    # this parent. It must not be mistaken for the main paper PDF (especially
    # when the supplement is smaller than the main-PDF minimum size). Ignore
    # only exact imported-file readbacks for filenames and hashes that came
    # from the verified bundle; every other PDF child keeps the strict legacy
    # main-PDF checks below.
    supplement_pdfs = {
        row.get("zotero_filename", row["filename"]).casefold(): row
        for row in verified_supplements
        if row.get("content_type") == "application/pdf"
        and isinstance(row.get("filename"), str)
    }
    matched, incomplete, different = [], [], False
    for child in children:
        data = child.get("data", {}) if isinstance(child, dict) else {}
        if data.get("contentType") != "application/pdf":
            continue
        key = _key(child.get("key"))
        if data.get("parentItem") != parent_key:
            raise FilingError("CHILD_PARENT_CONFLICT")
        filename = data.get("filename")
        supplement = (supplement_pdfs.get(filename.casefold())
                      if isinstance(filename, str)
                      and filename.casefold() != pdf.name.casefold() else None)
        if supplement is not None:
            if (data.get("linkMode") == "imported_file"
                    and data.get("contentType") == supplement["content_type"]
                    and data.get("md5") == supplement["md5"]):
                try:
                    current = _any_file_identity(_source_file(client, key, storage_root))
                except FilingError:
                    current = None
                if current == {"sha256": supplement["sha256"],
                               "md5": supplement["md5"],
                               "size": supplement["size_bytes"]}:
                    continue
            # The bundle says this filename is a supplement, but Zotero's
            # stored child is not an exact verified match. Do not treat it as
            # the main PDF or silently accept the collision.
            different = True
            continue
        if data.get("linkMode") not in {"imported_file", "imported_url"}:
            raise FilingError("PDF_ATTACHMENT_NOT_IMPORTED")
        if not data.get("md5") and data.get("filename") == pdf.name:
            incomplete.append(key)
            continue
        current = _file_identity(_source_file(client, key, storage_root))
        if current["sha256"] == identity["sha256"] and data.get("md5") == identity["md5"]:
            matched.append(key)
        else:
            different = True
    if len(matched) > 1 or len(incomplete) > 1:
        raise FilingError("AMBIGUOUS_PDF_ATTACHMENTS")
    if matched:
        return matched[0], True
    if different:
        raise FilingError("EXISTING_PDF_DIFFERS_REVIEW")
    return (incomplete[0], False) if incomplete else (None, False)


def supplement_attachment_plan(client, parent_key, supplement, storage_root):
    """Return an exact imported-file match, or fail on a filename collision."""
    children = client.get_json(f"/users/0/items/{parent_key}/children?format=json")
    if not isinstance(children, list):
        raise FilingError("INVALID_CHILDREN")
    filename = supplement.get("zotero_filename", supplement["filename"])
    matches = []
    for child in children:
        data = child.get("data", {}) if isinstance(child, dict) else {}
        if not isinstance(data, dict):
            continue
        if not isinstance(data.get("filename"), str) or data["filename"].casefold() != filename.casefold():
            continue
        if data.get("parentItem") != parent_key:
            raise FilingError("CHILD_PARENT_CONFLICT")
        key = _key(child.get("key"))
        if data.get("linkMode") != "imported_file":
            raise FilingError("SUPPLEMENT_ATTACHMENT_NOT_IMPORTED")
        stored = _any_file_identity(_source_file(client, key, storage_root))
        if (data.get("contentType") != supplement["content_type"]
                or data.get("md5") != supplement["md5"]
                or stored["sha256"] != supplement["sha256"]
                or stored["md5"] != supplement["md5"]
                or stored["size"] != supplement["size_bytes"]):
            raise FilingError("SUPPLEMENT_FILENAME_CONFLICT")
        matches.append(key)
    if len(matches) > 1:
        raise FilingError("AMBIGUOUS_SUPPLEMENT_ATTACHMENTS")
    return (matches[0], True) if matches else (None, False)


def _supplement_receipt(supplement, *, status, attachment_key=None, reason_code=None):
    row = {key: supplement[key] for key in
           ("relative_path", "filename", "content_type", "size_bytes", "sha256", "md5")}
    row["zotero_filename"] = supplement.get("zotero_filename", supplement["filename"])
    row["status"] = status
    if attachment_key is not None:
        row["attachment_key"] = _key(attachment_key)
    if reason_code is not None and re.fullmatch(r"[A-Z0-9_]+", str(reason_code)):
        row["reason_code"] = str(reason_code)
    return row


def _zotero_supplement_filenames(pdf, supplements):
    """Derive remote names without changing the verified local bundle names.

    Zotero stores sibling attachments by filename. A supplement whose local
    basename equals the main PDF therefore needs a distinct, deterministic
    name. If that prefix already belongs to another supplement, add a short
    content-hash suffix and extend it only if necessary.
    """
    main_name = pdf.name.casefold()
    used = {main_name}
    for row in supplements:
        filename = row["filename"]
        if filename.casefold() != main_name:
            used.add(filename.casefold())

    result = []
    for row in supplements:
        copy_row = dict(row)
        filename = row["filename"]
        remote_name = filename
        if filename.casefold() == main_name:
            remote_name = "supplement-" + filename
            if remote_name.casefold() in used:
                stem, suffix = os.path.splitext(remote_name)
                digest = row["sha256"]
                for width in (8, 12, 16, 24, 32, 48, 64):
                    candidate = f"{stem}-{digest[:width]}{suffix}"
                    if candidate.casefold() not in used:
                        remote_name = candidate
                        break
                else:
                    raise FilingError("SUPPLEMENT_FILENAME_MAPPING_CONFLICT")
            used.add(remote_name.casefold())
        copy_row["zotero_filename"] = remote_name
        result.append(copy_row)
    return result


def _write_bundle_zotero_receipt(pdf, *, server_id, parent_key, pdf_attachment_key,
                                 collection_key, collection_path, topic_keyword,
                                 supplement_source_status, supplement_status,
                                 supplement_receipts):
    manifest = read_bundle(pdf.parent)
    if manifest is None:
        return False
    # Re-validate the source bundle at receipt time. This binds the local
    # receipt to the exact files that were actually examined and uploaded.
    current = verified_supplements(pdf)
    current_by_path = {row["relative_path"]: row for row in current}
    for row in supplement_receipts:
        source = current_by_path.get(row["relative_path"])
        if (source is None or source["sha256"] != row["sha256"]
                or source["size_bytes"] != row["size_bytes"]):
            raise FilingError("BUNDLE_CHANGED_BEFORE_RECEIPT")
    manifest["zotero"] = {
        "status": "FILED",
        "server_id": server_id,
        "parent_key": _key(parent_key),
        "collection_key": _key(collection_key),
        "collection_path": collection_path,
        "topic_keyword": topic_keyword,
        "pdf": {"status": "FILED", "attachment_key": _key(pdf_attachment_key),
                "filename": pdf.name, "sha256": _file_identity(pdf)["sha256"]},
        "supplements": {"status": supplement_status,
                        "source_status": supplement_source_status,
                        "files": supplement_receipts},
    }
    write_bundle(pdf.parent, manifest)
    return True


def _plan_supplements(client, parent_key, supplements, storage_root):
    planned = []
    for supplement in supplements:
        if supplement["size_bytes"] > MAX_SUPPLEMENT_BYTES:
            planned.append(_supplement_receipt(
                supplement, status="unsupported", reason_code="SUPPLEMENT_SIZE_OUT_OF_SCOPE"))
            continue
        try:
            key, attached = (supplement_attachment_plan(client, parent_key, supplement, storage_root)
                             if parent_key is not None else (None, False))
        except FilingError as exc:
            planned.append(_supplement_receipt(
                supplement, status="needs_review", reason_code=str(exc)))
            continue
        planned.append(_supplement_receipt(
            supplement, status="already_filed" if attached else "will_import",
            attachment_key=key))
    return planned


def _bundle_context(pdf):
    try:
        manifest = read_bundle(pdf.parent)
        supplements = verified_supplements(pdf)
    except BundleError as exc:
        code = str(exc)
        raise FilingError(code if re.fullmatch(r"[A-Z0-9_]+", code)
                          else "BUNDLE_INVALID") from None
    source_status = "not_checked"
    if manifest is not None:
        details = manifest.get("supplements", {"status": "not_checked"})
        if not isinstance(details, dict) or details.get("status") not in {
                "not_checked", "partial", "complete", "not_applicable"}:
            raise FilingError("BUNDLE_SUPPLEMENTS_STATUS_INVALID")
        source_status = details["status"]
    return manifest, supplements, source_status


def _verify_supplement_readback(client, parent_key, attachment_key,
                                supplement, storage_root):
    attachment_key = _key(attachment_key)
    zotero_filename = supplement.get("zotero_filename", supplement["filename"])
    row = client.get_json(f"/users/0/items/{attachment_key}")
    data = row.get("data", {}) if isinstance(row, dict) else {}
    if (not isinstance(data, dict) or data.get("parentItem") != parent_key
            or data.get("linkMode") != "imported_file"
            or data.get("filename") != zotero_filename
            or data.get("contentType") != supplement["content_type"]
            or data.get("md5") != supplement["md5"]):
        raise FilingError("SUPPLEMENT_READBACK_FAILED")
    stored = _any_file_identity(_source_file(client, attachment_key, storage_root))
    if (stored["sha256"] != supplement["sha256"] or stored["md5"] != supplement["md5"]
            or stored["size"] != supplement["size_bytes"]):
        raise FilingError("SUPPLEMENT_READBACK_FAILED")
    return attachment_key


def _supplement_completion(source_status, receipts):
    if source_status == "not_applicable":
        return "not_applicable"
    if source_status == "not_checked" and not receipts:
        return "not_checked"
    if source_status != "complete":
        return "partial"
    if all(row.get("status") in {"FILED", "already_filed"} for row in receipts):
        return "complete"
    return "partial"


def _api_failure_code(exc):
    value = getattr(exc, "code", None)
    if not isinstance(value, str):
        value = str(exc) if isinstance(exc, FilingError) else "SUPPLEMENT_IMPORT_FAILED"
    return value if re.fullmatch(r"[A-Z0-9_]+", value) else "SUPPLEMENT_IMPORT_FAILED"


def bibliographic_data(candidate, metadata):
    if not isinstance(metadata, dict):
        raise FilingError("BIBLIOGRAPHIC_METADATA_REQUIRED")
    if normalize_title(metadata.get("title")) != normalize_title(candidate.get("title")):
        raise FilingError("METADATA_TITLE_CONFLICT")
    for key in ("doi", "pmcid"):
        if metadata.get(key) and str(metadata[key]).lower() != str(candidate.get(key, "")).lower():
            raise FilingError("METADATA_IDENTIFIER_CONFLICT")
    authors = metadata.get("authors")
    if (not isinstance(authors, list) or not 1 <= len(authors) <= 500
            or any(not isinstance(a, str) or not a.strip() or len(a) > 500 for a in authors)):
        raise FilingError("METADATA_AUTHORS_REQUIRED")
    if any(not isinstance(metadata.get(k), str) or not metadata[k].strip() or len(metadata[k]) > 500
           for k in ("published", "journal")):
        raise FilingError("METADATA_DATE_JOURNAL_REQUIRED")
    if candidate["source"] == "pubmed":
        url = f"https://pubmed.ncbi.nlm.nih.gov/{candidate['id']}/"
    elif candidate["source"] == "doi":
        url = f"https://doi.org/{candidate['doi']}"
    else:
        url = f"https://arxiv.org/abs/{candidate['id']}"
    extra = []
    if candidate.get("id") and candidate["source"] == "pubmed":
        extra.append("PMID: " + candidate["id"])
    if candidate.get("pmcid"):
        extra.append("PMCID: " + candidate["pmcid"])
    if candidate["source"] == "arxiv":
        extra.append("arXiv: " + candidate["id"])
    publication_type = metadata.get("publication_type", "journalArticle")
    if publication_type not in ("journalArticle", "preprint"):
        raise FilingError("METADATA_PUBLICATION_TYPE_INVALID")
    citation = {"itemType": publication_type, "title": metadata["title"],
            "creators": [{"creatorType": "author", "name": a} for a in authors],
            "date": metadata["published"], "publicationTitle": metadata["journal"],
            "DOI": candidate.get("doi", ""), "url": url,
            "extra": "\n".join(extra),
            "tags": [], "relations": {}}
    if publication_type == "preprint":
        citation["repository"] = citation.pop("publicationTitle")
        citation["genre"] = "Preprint"
    return citation


def _created_key(answer):
    if not isinstance(answer, dict) or answer.get("failed"):
        raise FilingError("CREATE_RESULT_FAILED")
    result = (answer.get("successful") or answer.get("success") or {}).get("0")
    return _key(result.get("key") if isinstance(result, dict) else result)


def _state(root, server_id):
    path = root / FILING_STATE
    state = _json(path) if path.exists() else {"version": 1, "server_id": server_id, "jobs": {}}
    if state.get("server_id") != server_id or not isinstance(state.get("jobs"), dict):
        raise FilingError("FILING_STATE_SERVER_CONFLICT")
    return state


def _save(root, state):
    path = root / FILING_STATE
    if path.is_symlink():
        raise FilingError("UNSAFE_FILING_STATE")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=root, prefix=".paper-zotero-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def file_saved_job(directory, job_key, *, client, metadata=None, collection=None,
                   storage_root=DEFAULT_STORAGE, execute=False, candidate_lookup=None):
    root, job, candidate, pdf, identity = verified_job(directory, job_key)
    _manifest, supplements, supplement_source_status = _bundle_context(pdf)
    supplements = _zotero_supplement_filenames(pdf, supplements)
    client.discover()
    server_id = client.server_id
    if not isinstance(server_id, str) or not server_id:
        raise FilingError("SERVER_ID_REQUIRED")
    state = _state(root, server_id)
    previous = state["jobs"].get(job_key)
    if job_key in state["jobs"] and (not isinstance(previous, dict) or previous.get("source_pdf_sha256") != identity["sha256"]):
        raise FilingError("FILING_STATE_PDF_CONFLICT")
    if candidate_lookup is not None:
        if not callable(candidate_lookup):
            raise FilingError("INVALID_CANDIDATE_LOOKUP")
        items = candidate_lookup(candidate)
        if not isinstance(items, list):
            raise FilingError("INVALID_CANDIDATE_LOOKUP")
    else:
        items, _ = snapshot(client, "items")
    collections, _ = snapshot(client, "collections")
    try:
        topic = choose_topic(candidate.get("title"), job.get("query"), collections, explicit=collection)
    except ValueError as exc:
        raise FilingError(str(exc)) from None
    name = collection_name(topic["name"])
    parent = _choose_parent(candidate, items)
    coll = _choose_collection(name, collections)
    attachment, attached = attachment_plan(
        client, parent, pdf, identity, storage_root, verified_supplements=supplements)
    citation = bibliographic_data(candidate, metadata if metadata is not None else job.get("bibliography")) if parent is None else None
    parent_data = client.get_json(f"/users/0/items/{parent}")["data"] if parent else {}
    memberships = parent_data.get("collections", [])
    if not isinstance(memberships, list) or any(not KEY.fullmatch(str(k)) for k in memberships):
        raise FilingError("INVALID_COLLECTION_MEMBERSHIP")
    supplement_plan = _plan_supplements(client, parent, supplements, storage_root)
    result = {"mode": "execute" if execute else "dry-run", "job_key": job_key,
              "source_pdf_sha256": identity["sha256"], "pdf_size_bytes": identity["size"],
              "collection_name": name, "parent_key": parent, "collection_key": coll,
              "topic_keyword": topic["topic_keyword"], "collection_path": topic["path"],
              "pdf_attachment_key": attachment, "create_parent": parent is None,
              "create_collection": coll is None, "attach_pdf": not attached,
              "add_membership": parent is not None and coll not in memberships,
              "zotero_write_attempted": False, "zotero_write_uncertain": False,
              "pdf_status": "already_filed" if attached else "will_file",
              "supplements_source_status": supplement_source_status,
              "supplements_status": "planned" if supplements else supplement_source_status,
              "supplement_attachments": supplement_plan,
              "status": "READY", "sidecar_written": False,
              "bundle_receipt_written": False}
    if not execute:
        return result
    # Same installation lock as the downloader prevents concurrent automated
    # saves. API version preconditions protect against unrelated Zotero edits.
    with process_lock(Path(__file__).with_name("paper_downloader-global")):
        state = _state(root, server_id)
        previous = state["jobs"].get(job_key)
        if job_key in state["jobs"] and (not isinstance(previous, dict) or previous.get("source_pdf_sha256") != identity["sha256"]):
            raise FilingError("FILING_STATE_PDF_CONFLICT")
        fresh = verified_job(root, job_key)
        if fresh[2] != candidate or fresh[4] != identity:
            raise FilingError("JOB_CHANGED_BEFORE_WRITE")
        _fresh_manifest, fresh_supplements, fresh_source_status = _bundle_context(pdf)
        fresh_supplements = _zotero_supplement_filenames(pdf, fresh_supplements)
        if (fresh_source_status != supplement_source_status
                or [(s["relative_path"], s["filename"], s["zotero_filename"], s["content_type"],
                     s["size_bytes"], s["sha256"], s["md5"])
                    for s in fresh_supplements]
                != [(s["relative_path"], s["filename"], s["zotero_filename"], s["content_type"],
                     s["size_bytes"], s["sha256"], s["md5"])
                    for s in supplements]):
            raise FilingError("BUNDLE_CHANGED_BEFORE_WRITE")
        supplements = fresh_supplements
        record = {"status": "PARTIAL", "server_id": server_id, "source_pdf_sha256": identity["sha256"],
                  "topic_keyword": topic["topic_keyword"], "collection_path": topic["path"],
                  "parent_key": parent, "collection_key": coll, "pdf_attachment_key": attachment,
                  "zotero_write_attempted": False, "zotero_write_uncertain": False}
        state["jobs"][job_key] = record
        mutation_count = 0

        def checkpoint():
            record["updated_at"] = datetime.now(timezone.utc).isoformat()
            _save(root, state)

        def mutate(call):
            nonlocal mutation_count
            # A remembered grant is reused across this batch and recovery
            # processes. A failed write is never replayed blindly.
            client.authorize()
            record["zotero_write_attempted"] = result["zotero_write_attempted"] = True
            record["zotero_write_uncertain"] = result["zotero_write_uncertain"] = True
            mutation_count += 1
            checkpoint()
            return call()

        def upload_bytes(url, payload, content_type):
            nonlocal mutation_count
            record["zotero_write_attempted"] = result["zotero_write_attempted"] = True
            record["zotero_write_uncertain"] = result["zotero_write_uncertain"] = True
            mutation_count += 1
            checkpoint()
            return client.upload_file(url, payload, content_type)

        try:
            # Revalidate input immediately before the first potential mutation.
            if _file_identity(pdf) != identity:
                raise FilingError("SAVED_PDF_CHANGED")
            if coll is not None:
                coll_row = client.get_json(f"/users/0/collections/{coll}")
                if normalize_title(coll_row.get("data", {}).get("name")) != normalize_title(name):
                    raise FilingError("COLLECTION_CHANGED_BEFORE_WRITE")
            if coll is None:
                rows, version = snapshot(client, "collections")
                coll = _choose_collection(name, rows)
                if coll is None:
                    coll = _created_key(mutate(lambda: client.write_json("POST", "/users/0/collections",
                                       [{"name": name, "parentCollection": False}],
                                       headers={"If-Unmodified-Since-Version": version})))
                record["collection_key"] = coll
                checkpoint()
            if parent is None:
                rows, version = candidate_snapshot(client, candidate, candidate_lookup)
                parent = _choose_parent(candidate, rows)
                if parent is None:
                    citation["collections"] = [coll]
                    parent = _created_key(mutate(lambda: client.write_json("POST", "/users/0/items", [citation],
                                         headers={"If-Unmodified-Since-Version": version})))
                record["parent_key"] = parent
                checkpoint()
            parent_row = client.get_json(f"/users/0/items/{parent}")
            if _choose_parent(candidate, [parent_row]) != parent:
                raise FilingError("PARENT_IDENTITY_CHANGED")
            current = parent_row.get("data", {}).get("collections", [])
            if not isinstance(current, list) or any(not isinstance(k, str) or not KEY.fullmatch(k) for k in current):
                raise FilingError("INVALID_COLLECTION_MEMBERSHIP")
            if coll not in current:
                version = parent_row.get("version")
                if type(version) is not int or version < 0:
                    raise FilingError("ITEM_VERSION_REQUIRED")
                mutate(lambda: client.write_json("PATCH", f"/users/0/items/{parent}", {"collections": current + [coll]},
                       headers={"If-Unmodified-Since-Version": str(version)}))
            attachment, attached = attachment_plan(
                client, parent, pdf, identity, storage_root, verified_supplements=supplements)
            if not attached:
                if attachment is None:
                    rows, version = candidate_snapshot(client, candidate, candidate_lookup)
                    if _choose_parent(candidate, rows) != parent:
                        raise FilingError("PARENT_IDENTITY_CHANGED")
                    attachment, attached = attachment_plan(
                        client, parent, pdf, identity, storage_root,
                        verified_supplements=supplements)
                    if attachment is None:
                        attachment = _created_key(mutate(lambda: client.write_json("POST", "/users/0/items", [{
                            "itemType": "attachment", "parentItem": parent, "linkMode": "imported_file",
                            "title": pdf.stem, "filename": pdf.name, "contentType": "application/pdf",
                            "tags": [], "relations": {}}], headers={"If-Unmodified-Since-Version": version})))
                record["pdf_attachment_key"] = attachment
                checkpoint()
            if not attached:
                endpoint = f"/users/0/items/{attachment}/file"
                auth = mutate(lambda: client.write_form(endpoint, {"md5": identity["md5"], "filename": pdf.name,
                              "filesize": str(identity["size"]), "mtime": str(int(pdf.stat().st_mtime * 1000))},
                              headers={"If-None-Match": "*"}))
                if not isinstance(auth, dict):
                    raise FilingError("INVALID_UPLOAD_AUTHORIZATION")
                if not auth.get("exists"):
                    if (not isinstance(auth.get("uploadKey"), str) or not auth["uploadKey"]
                            or not isinstance(auth.get("url"), str) or auth.get("prefix", "") or auth.get("suffix", "")):
                        raise FilingError("INVALID_LOCAL_UPLOAD_AUTHORIZATION")
                    if _file_identity(pdf) != identity:
                        raise FilingError("SAVED_PDF_CHANGED")
                    upload_bytes(auth["url"], pdf.read_bytes(), auth.get("contentType", "application/octet-stream"))
                    mutate(lambda: client.write_form(endpoint, {"upload": auth["uploadKey"]}, headers={"If-None-Match": "*"}))
            # Mandatory readback: identifiers, classification, imported bytes.
            parent_row = client.get_json(f"/users/0/items/{parent}")
            if _choose_parent(candidate, [parent_row]) != parent or coll not in parent_row.get("data", {}).get("collections", []):
                raise FilingError("PARENT_COLLECTION_READBACK_FAILED")
            coll_row = client.get_json(f"/users/0/collections/{coll}")
            if normalize_title(coll_row.get("data", {}).get("name")) != normalize_title(name):
                raise FilingError("COLLECTION_READBACK_FAILED")
            attachment, complete = attachment_plan(
                client, parent, pdf, identity, storage_root,
                verified_supplements=supplements)
            if not complete or _file_identity(pdf) != identity:
                raise FilingError("PDF_READBACK_FAILED")
            record.update({"status": "FILED", "parent_key": parent, "collection_key": coll,
                           "pdf_attachment_key": attachment, "zotero_write_uncertain": False})
            checkpoint()
            result.update({"status": "FILED", "parent_key": parent, "collection_key": coll,
                           "pdf_attachment_key": attachment, "pdf_status": "FILED",
                           "zotero_write_uncertain": False,
                           "sidecar_written": True})

            # The PDF is now independently verified and checkpointed. A
            # supplement failure can therefore leave this parent and PDF filed.
            supplement_receipts = []
            stop_after_uncertain = False
            for supplement in supplements:
                if supplement["size_bytes"] > MAX_SUPPLEMENT_BYTES:
                    supplement_receipts.append(_supplement_receipt(
                        supplement, status="unsupported", reason_code="SUPPLEMENT_SIZE_OUT_OF_SCOPE"))
                    continue
                if stop_after_uncertain:
                    supplement_receipts.append(_supplement_receipt(
                        supplement, status="not_attempted", reason_code="EARLIER_SUPPLEMENT_UNCERTAIN"))
                    continue
                attempts_before = mutation_count
                attachment_key = None
                try:
                    attachment_key, attached = supplement_attachment_plan(
                        client, parent, supplement, storage_root)
                    if not attached:
                        rows, version = candidate_snapshot(client, candidate, candidate_lookup)
                        if _choose_parent(candidate, rows) != parent:
                            raise FilingError("PARENT_IDENTITY_CHANGED")
                        attachment_key, attached = supplement_attachment_plan(
                            client, parent, supplement, storage_root)
                        if not attached:
                            if attachment_key is not None:
                                raise FilingError("SUPPLEMENT_ATTACHMENT_INCOMPLETE_REVIEW")
                            zotero_filename = supplement.get(
                                "zotero_filename", supplement["filename"])
                            attachment_key = _created_key(mutate(lambda: client.write_json(
                                "POST", "/users/0/items", [{
                                    "itemType": "attachment", "parentItem": parent,
                                    "linkMode": "imported_file", "title": Path(zotero_filename).stem,
                                    "filename": zotero_filename,
                                    "contentType": supplement["content_type"], "tags": [], "relations": {}}],
                                headers={"If-Unmodified-Since-Version": version})))
                            checkpoint()
                        if not attached:
                            endpoint = f"/users/0/items/{attachment_key}/file"
                            source_path = supplement["path"]
                            if _any_file_identity(source_path) != {
                                    "sha256": supplement["sha256"], "md5": supplement["md5"],
                                    "size": supplement["size_bytes"]}:
                                raise FilingError("BUNDLE_SUPPLEMENT_CHANGED")
                            auth = mutate(lambda: client.write_form(endpoint, {
                                "md5": supplement["md5"], "filename": zotero_filename,
                                "filesize": str(supplement["size_bytes"]),
                                "mtime": str(int(source_path.stat().st_mtime * 1000))},
                                headers={"If-None-Match": "*"}))
                            if not isinstance(auth, dict):
                                raise FilingError("INVALID_UPLOAD_AUTHORIZATION")
                            if not auth.get("exists"):
                                if (not isinstance(auth.get("uploadKey"), str) or not auth["uploadKey"]
                                        or not isinstance(auth.get("url"), str)
                                        or auth.get("prefix", "") or auth.get("suffix", "")):
                                    raise FilingError("INVALID_LOCAL_UPLOAD_AUTHORIZATION")
                                if _any_file_identity(source_path) != {
                                        "sha256": supplement["sha256"], "md5": supplement["md5"],
                                        "size": supplement["size_bytes"]}:
                                    raise FilingError("BUNDLE_SUPPLEMENT_CHANGED")
                                content_type = auth.get("contentType", "application/octet-stream")
                                if (not isinstance(content_type, str)
                                        or not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", content_type)):
                                    raise FilingError("INVALID_LOCAL_UPLOAD_AUTHORIZATION")
                                upload_bytes(auth["url"], source_path.read_bytes(), content_type)
                                mutate(lambda: client.write_form(
                                    endpoint, {"upload": auth["uploadKey"]},
                                    headers={"If-None-Match": "*"}))
                    attachment_key = _verify_supplement_readback(
                        client, parent, attachment_key, supplement, storage_root)
                    supplement_receipts.append(_supplement_receipt(
                        supplement, status="FILED", attachment_key=attachment_key))
                except Exception as exc:
                    reason = _api_failure_code(exc)
                    if mutation_count > attempts_before:
                        status = "uncertain"
                        stop_after_uncertain = True
                        record["zotero_write_uncertain"] = result["zotero_write_uncertain"] = True
                    else:
                        status = "needs_review"
                    supplement_receipts.append(_supplement_receipt(
                        supplement, status=status, attachment_key=attachment_key,
                        reason_code=reason))
                checkpoint()

            supplement_status = _supplement_completion(supplement_source_status, supplement_receipts)
            result["supplements_status"] = supplement_status
            result["supplement_attachments"] = supplement_receipts
            if _manifest is not None:
                result["bundle_receipt_written"] = _write_bundle_zotero_receipt(
                    pdf, server_id=server_id, parent_key=parent, pdf_attachment_key=attachment,
                    collection_key=coll, collection_path=topic["path"],
                    topic_keyword=topic["topic_keyword"],
                    supplement_source_status=supplement_source_status,
                    supplement_status=supplement_status,
                    supplement_receipts=supplement_receipts)
            if not stop_after_uncertain:
                record["zotero_write_uncertain"] = result["zotero_write_uncertain"] = False
            record.update({"status": "FILED", "parent_key": parent, "collection_key": coll,
                           "pdf_attachment_key": attachment})
            checkpoint()
            result["zotero_write_uncertain"] = record["zotero_write_uncertain"]
            result["sidecar_written"] = True
            return result
        except Exception:
            # Keep partial objects and conservative write uncertainty. Recovery
            # starts from a fresh API snapshot rather than trusting stored keys.
            checkpoint()
            raise


def file_existing_parent_bundle(pdf, parent_key, doi, collection_key, *, client, storage_root):
    """File only missing supplements under a pre-existing verified parent.

    Uses the standard attachment planner/upload/readback; creates no parent,
    collection or membership. Unknown writes persist in the existing bundle.
    """
    pdf = Path(pdf)
    manifest, supplements, source_status = _bundle_context(pdf)
    if manifest is None:
        raise FilingError("BUNDLE_REQUIRED")
    parent = client.get_json(f"/users/0/items/{_key(parent_key)}")
    data = parent["data"]
    if data.get("DOI", "").lower().strip() != doi.lower() or collection_key not in data.get("collections", []):
        raise FilingError("PARENT_IDENTITY_CHANGED")
    binding = manifest.get("zotero", {})
    if binding.get("parent_key") != parent_key or binding.get("pdf", {}).get("sha256") != _file_identity(pdf)["sha256"]:
        raise FilingError("BUNDLE_PARENT_BINDING_DIFFERS")
    receipts = []
    for supplement in _zotero_supplement_filenames(pdf, supplements):
        receipt = _supplement_receipt(supplement, status="pending")
        previous = next((r for r in binding.get("supplements", {}).get("files", [])
                         if r.get("relative_path") == supplement["relative_path"]), {})
        key = previous.get("attachment_key")
        attempted = previous.get("status") == "uncertain"
        def checkpoint():
            current = read_bundle(pdf.parent)
            current.setdefault("zotero", {}).setdefault("supplements", {})["files"] = receipts + [receipt] + [
                r for r in binding.get("supplements", {}).get("files", [])
                if r.get("relative_path") not in {x["relative_path"] for x in receipts + [receipt]}]
            write_bundle(pdf.parent, current)
        try:
            key, attached = supplement_attachment_plan(client, parent_key, supplement, storage_root)
            if not attached:
                if previous.get("status") == "uncertain":
                    raise FilingError("UNCERTAIN_ATTACHMENT_REQUIRES_RECONCILIATION")
                # The loopback client returns response headers from request(),
                # but does not expose a mutable last_library_version property.
                # Bracket a fresh parent/children read with global versions and
                # use that version for Zotero's optimistic write precondition.
                version_before = _current_library_version(client)
                fresh_parent = client.get_json(f"/users/0/items/{parent_key}")
                fresh_data = fresh_parent.get("data", {}) if isinstance(fresh_parent, dict) else {}
                if (fresh_data.get("DOI", "").lower().strip() != doi.lower()
                        or collection_key not in fresh_data.get("collections", [])):
                    raise FilingError("PARENT_IDENTITY_CHANGED")
                key, attached = supplement_attachment_plan(client, parent_key, supplement, storage_root)
                version_after = _current_library_version(client)
                if version_before != version_after:
                    raise FilingError("LIBRARY_CHANGED_DURING_SCAN")
                if not attached:
                    client.authorize()
                    receipt.update(status="uncertain", reason_code="ATTACHMENT_CREATE_INTENT")
                    checkpoint();attempted = True
                    name = supplement.get("zotero_filename", supplement["filename"])
                    key = _created_key(client.write_json("POST", "/users/0/items", [{"itemType": "attachment",
                        "parentItem": parent_key, "linkMode": "imported_file", "title": Path(name).stem,
                        "filename": name, "contentType": supplement["content_type"], "tags": [], "relations": {}}],
                        headers={"If-Unmodified-Since-Version": version_after}))
                    receipt["attachment_key"] = key;checkpoint()
                    endpoint = f"/users/0/items/{key}/file"
                    authorization = client.write_form(endpoint, {"md5": supplement["md5"], "filename": name,
                        "filesize": str(supplement["size_bytes"]), "mtime": str(int(supplement["path"].stat().st_mtime * 1000))},
                        headers={"If-None-Match": "*"})
                    if not authorization.get("exists"):
                        if authorization.get("prefix") or authorization.get("suffix") or not authorization.get("uploadKey"):
                            raise FilingError("INVALID_LOCAL_UPLOAD_AUTHORIZATION")
                        client.upload_file(authorization["url"], supplement["path"].read_bytes(),
                                           authorization.get("contentType", "application/octet-stream"))
                        client.write_form(endpoint, {"upload": authorization["uploadKey"]}, headers={"If-None-Match": "*"})
            key = _verify_supplement_readback(client, parent_key, key, supplement, storage_root)
            receipt.update(status="FILED", attachment_key=key, reason_code=None)
        except (ValueError, OSError, RuntimeError) as error:
            receipt.update(status="uncertain" if attempted else "needs_review", attachment_key=key, reason_code=str(error))
            checkpoint()
            receipts.append(receipt)
            if attempted:
                raise FilingError("ATTACHMENT_WRITE_UNCERTAIN") from error
            continue
        checkpoint();receipts.append(receipt)
    current = read_bundle(pdf.parent)
    current["zotero"]["supplements"] = {"files": receipts, "source_status": source_status,
        "status": "FILED" if all(r["status"] == "FILED" for r in receipts) else "partial"}
    write_bundle(pdf.parent, current)
    return receipts


def prepare_saved_paper(directory, paper):
    """Create or verify one DOI/arXiv SAVED job from an already indexed PDF.

    This is a local-only bridge for a publisher/DOI record that already exists
    in the download ledger. It never downloads a file or contacts Zotero.
    """
    raw_root = Path(directory)
    if raw_root.is_symlink() or not raw_root.is_dir():
        raise FilingError("INVALID_DOWNLOAD_DIRECTORY")
    root = raw_root.resolve(strict=True)
    if not isinstance(paper, dict) or paper.get("source") not in {"doi", "arxiv"}:
        raise FilingError("INVALID_PUBLICATION_JOB")
    source, source_id = paper.get("source"), paper.get("id")
    candidate = {"source": source, "id": source_id, "title": paper.get("title")}
    if source == "doi":
        doi = normalized_doi(source_id)
        if not doi or doi != source_id or normalized_doi(paper.get("doi")) != doi:
            raise FilingError("INVALID_DOI_JOB")
        candidate["doi"] = doi
    else:
        if (not isinstance(source_id, str)
                or not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[A-Za-z.-]+/\d{7})(?:v\d+)?", source_id)):
            raise FilingError("INVALID_ARXIV_JOB")
        candidate["id"] = source_id.lower()
        doi = normalized_doi(paper.get("doi")) if paper.get("doi") else None
        if paper.get("doi") and not doi:
            raise FilingError("INVALID_DOI_JOB")
        if doi:
            candidate["doi"] = doi
    if paper.get("pmcid"):
        candidate["pmcid"] = paper["pmcid"]
    if not isinstance(paper.get("title"), str) or not paper["title"].strip():
        raise FilingError("BIBLIOGRAPHIC_METADATA_REQUIRED")
    # Validate the complete public citation before writing a download job.
    bibliographic_data(candidate, paper)

    jobs_path, ledger_path = root / ".paper-download-jobs.json", root / ".paper-download-ledger.json"
    try:
        DownloadLedger._regular_chain(ledger_path)
        if jobs_path.exists() or jobs_path.is_symlink():
            DownloadLedger._regular_chain(jobs_path)
        ledger = DownloadLedger(root)
        known = ledger.lookup(candidate)
    except (LedgerConflict, OSError):
        raise FilingError("LEDGER_CONFLICT") from None
    if known is None:
        raise FilingError("SAVED_PDF_REQUIRED")

    with process_lock(Path(__file__).with_name("paper_downloader-global")):
        try:
            DownloadLedger._regular_chain(ledger_path)
            if jobs_path.exists() or jobs_path.is_symlink():
                DownloadLedger._regular_chain(jobs_path)
            ledger = DownloadLedger(root)
            known = ledger.lookup(candidate)
        except (LedgerConflict, OSError):
            raise FilingError("LEDGER_CONFLICT") from None
        if known is None:
            raise FilingError("SAVED_PDF_REQUIRED")
        try:
            store = DownloadJobStore(root)
            query = paper.get("query") if isinstance(paper.get("query"), str) else "local saved-paper preparation"
            key = store.ensure(paper, query)
            may_prepare = store.prepare(key, paper, ledger)
            if may_prepare:
                store.mark_fetching(key)
                store.record_result(key, {**paper, **known, "status": "exists"}, ledger)
            elif paper.get("status") != "exists":
                raise FilingError("SAVED_JOB_REQUIRES_REVIEW")
        except FilingError:
            raise
        except (ValueError, LedgerConflict, OSError):
            raise FilingError("SAVED_JOB_PREPARATION_FAILED") from None
        verified = verified_job(root, key)
        if verified[4]["sha256"] != known["sha256"]:
            raise FilingError("SAVED_PDF_CHANGED")
        return {"status": "SAVED", "job_key": key,
                "pdf_filename": verified[3].name, "pdf_sha256": verified[4]["sha256"],
                "pdf_size_bytes": verified[4]["size"], "zotero_write_attempted": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download-dir", type=Path, default=default_path("download_dir", "data/papers"))
    parser.add_argument("--job-key")
    parser.add_argument("--metadata", type=Path, help="saved public bibliographic JSON, no API credentials")
    parser.add_argument("--prepare-saved", action="store_true",
                        help="register an already ledger-verified DOI PDF as SAVED without downloading or contacting Zotero")
    parser.add_argument("--doi", help="DOI to prepare with --prepare-saved")
    parser.add_argument("--collection", help="explicit keyword collection; default derives saved query")
    parser.add_argument("--storage-root", type=Path, default=DEFAULT_STORAGE)
    parser.add_argument("--execute", action="store_true", help="request user authorization for only this saved job")
    args = parser.parse_args(argv)
    try:
        metadata = json.loads(args.metadata.read_text(encoding="utf-8")) if args.metadata else None
        if args.prepare_saved:
            if args.execute or args.job_key or not args.doi or not isinstance(metadata, dict):
                raise FilingError("INVALID_PREPARE_OPTIONS")
            paper = dict(metadata)
            paper.update({"source": "doi", "id": args.doi,
                          "doi": paper.get("doi", args.doi)})
            answer = prepare_saved_paper(args.download_dir, paper)
            print(json.dumps(answer, ensure_ascii=False, indent=2))
            return 0
        if not args.job_key:
            raise FilingError("JOB_KEY_REQUIRED")
        from zotero_local_api import ZoteroLocalClient
        from zotero_candidate_lookup import CandidateLookup
        client = ZoteroLocalClient()
        answer = file_saved_job(args.download_dir, args.job_key, client=client, metadata=metadata,
                                collection=args.collection, storage_root=args.storage_root, execute=args.execute,
                                candidate_lookup=CandidateLookup(client))
    except Exception as exc:
        code = str(exc) if isinstance(exc, FilingError) else getattr(exc, "code", "FILING_FAILED")
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9_]+", code):
            code = "FILING_FAILED"
        print(json.dumps({"status": "blocked", "reason_code": code, "filed": False}), file=sys.stderr)
        return 2
    print(json.dumps(answer, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
