#!/usr/bin/env python3
"""Bounded SAVED PubMed filing queue; preview is the default.

Execution requires both explicit switches. Each job uses the existing filing
driver, including its production lock, Server-ID binding and write checkpoints.
Only verified FILED jobs are eligible for downstream processing.
"""
from __future__ import annotations

import argparse
import json
import re
import urllib.parse
from datetime import datetime
from pathlib import Path
from pdf_article_identity import ARTICLE_IDENTITY_CODES

from zotero_filing import DEFAULT_STORAGE, FILING_STATE, FilingError, file_saved_job
from zotero_local_api import ZoteroApiFailure, ZoteroLocalClient

_JOB_KEY = re.compile(r"[a-z][a-z0-9_-]{0,31}:[A-Za-z0-9._/-]{1,80}\Z")
_OBJECT_KEY = re.compile(r"[A-Z0-9]{8}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_STATE_LIMIT = 64 * 1024 * 1024
_METADATA_LIMIT = 64 * 1024
_ACTIONS = ("create_parent", "create_collection", "attach_pdf", "add_membership")
_OBJECTS = ("parent_key", "collection_key", "pdf_attachment_key")
_PUBLIC_METADATA = {"title", "authors", "published", "journal", "doi", "pmcid", "publication_type"}
_RECORD_REQUIRED = {"status", "server_id", "source_pdf_sha256", *_OBJECTS}
_RECORD_AUDIT = {"zotero_write_attempted", "zotero_write_uncertain", "updated_at",
                 "topic_keyword", "collection_path"}
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})\Z")
_QUEUE_CODES = frozenset({
    "INVALID_MAXIMUM", "INVALID_EXECUTION_FLAGS", "ZOTERO_WRITE_CONFIRMATION_REQUIRED",
    "INVALID_LOCAL_STATE", "INVALID_DOWNLOAD_DIRECTORY", "INCOMPLETE_DOWNLOAD_STATE",
    "INVALID_DOWNLOAD_STATE", "INVALID_JOB_KEYS", "INVALID_METADATA", "UNSUPPORTED_SOURCE",
    "METADATA_REQUIRES_UNIQUE_JOB", "FILING_FAILED", "FILING_STATE_READBACK_FAILED",
})

# Literal allowlists, not a regex accepting arbitrary exception text or tokens.
_DRIVER_CODES = frozenset({
    "INVALID_OBJECT_KEY", "MISSING_OR_UNSAFE_STATE", "INVALID_LOCAL_JSON",
    "UNSUPPORTED_STATE", "MISSING_OR_UNSAFE_PDF", "PDF_SIZE_OUT_OF_SCOPE",
    "INVALID_PDF_HEADER", "INVALID_DOWNLOAD_DIRECTORY", "INVALID_DOWNLOAD_STATE",
    "JOB_NOT_SAVED", "INVALID_PUBMED_JOB", "INVALID_PDF_FILENAME",
    "PDF_OUTSIDE_DOWNLOAD_DIRECTORY", "SAVED_PDF_CHANGED",
    "INVALID_PUBLICATION_IDENTIFIERS", "CONFLICTING_PUBLICATION_IDENTIFIERS",
    "LEDGER_CONFLICT", "UNCONFIRMED_SAVED_PDF", "MISSING_COLLECTION_QUERY",
    "INVALID_COLLECTION_NAME", "SNAPSHOT_TIME_BUDGET", "INVALID_SNAPSHOT_VERSION",
    "LIBRARY_CHANGED_DURING_SCAN", "INVALID_SNAPSHOT_JSON", "INVALID_SNAPSHOT_PAGE",
    "INVALID_SNAPSHOT_OBJECT", "DUPLICATE_SNAPSHOT_KEY", "SNAPSHOT_TOO_LARGE",
    "AMBIGUOUS_PARENT_MATCH", "AMBIGUOUS_COLLECTION_NAME", "ATTACHMENT_PATH_UNAVAILABLE",
    "INVALID_ATTACHMENT_PATH", "NONLOCAL_ATTACHMENT_PATH", "ATTACHMENT_FILE_UNAVAILABLE",
    "ATTACHMENT_OUTSIDE_STORAGE", "INVALID_CHILDREN", "CHILD_PARENT_CONFLICT",
    "PDF_ATTACHMENT_NOT_IMPORTED", "AMBIGUOUS_PDF_ATTACHMENTS", "EXISTING_PDF_DIFFERS_REVIEW",
    "BIBLIOGRAPHIC_METADATA_REQUIRED", "METADATA_TITLE_CONFLICT", "METADATA_IDENTIFIER_CONFLICT",
    "METADATA_AUTHORS_REQUIRED", "METADATA_DATE_JOURNAL_REQUIRED", "CREATE_RESULT_FAILED",
    "FILING_STATE_SERVER_CONFLICT", "UNSAFE_FILING_STATE", "SERVER_ID_REQUIRED",
    "FILING_STATE_PDF_CONFLICT", "JOB_CHANGED_BEFORE_WRITE", "COLLECTION_CHANGED_BEFORE_WRITE",
    "PARENT_IDENTITY_CHANGED", "INVALID_COLLECTION_MEMBERSHIP", "ITEM_VERSION_REQUIRED",
    "INVALID_UPLOAD_AUTHORIZATION", "INVALID_LOCAL_UPLOAD_AUTHORIZATION",
    "PARENT_COLLECTION_READBACK_FAILED", "COLLECTION_READBACK_FAILED", "PDF_READBACK_FAILED",
})
_API_CODES = frozenset({
    "API_ERROR", "API_UNAVAILABLE", "AUTH_DENIED", "AUTH_REQUIRED", "INVALID_JSON",
    "INVALID_REQUEST", "INVALID_RESPONSE", "INVALID_UPLOAD_URL", "PRECONDITION_FAILED",
    "RATE_LIMITED", "REDIRECT_REJECTED", "RESPONSE_TOO_LARGE", "SERVER_ID_MISMATCH",
    "TIMEOUT", "UNSUPPORTED_API_VERSION", "UPLOAD_FAILED",
    "AUTH_STORE_UNAVAILABLE", "AUTH_STORE_INVALID",
})

# Stable body-identity failures from the publisher-aware SAVED gate are public
# diagnostics, not generic retryable filing failures or raw exception strings.
_DRIVER_CODES = _DRIVER_CODES | ARTICLE_IDENTITY_CODES | {
    "TOPIC_REVIEW_REQUIRED", "INVALID_COLLECTION_TREE"}


class QueueError(RuntimeError):
    """Internal errors are constructed only with fixed public codes."""


def _reason(exc):
    if isinstance(exc, QueueError) and str(exc) in _QUEUE_CODES:
        return str(exc)
    if isinstance(exc, FilingError) and str(exc) in _DRIVER_CODES:
        return str(exc)
    if isinstance(exc, ZoteroApiFailure) and isinstance(exc.code, str) and exc.code in _API_CODES:
        return exc.code
    return "FILING_FAILED"


def _result(execute):
    return {"mode": "execute" if execute is True else "preview", "queue_initialized": False,
            "selected_job_keys": [], "eligible_job_keys": [], "planned": [], "processed": [],
            "failures": [], "zotero_write_attempted": False, "zotero_write_uncertain": False,
            "prior_zotero_write_attempted": False, "prior_zotero_write_uncertain": False,
            "deferred_by_limit": 0}


def _options(maximum, execute, confirm_zotero_write):
    if type(maximum) is not int or not 1 <= maximum <= 10:
        raise QueueError("INVALID_MAXIMUM")
    if type(execute) is not bool or type(confirm_zotero_write) is not bool:
        raise QueueError("INVALID_EXECUTION_FLAGS")
    if execute and not confirm_zotero_write:
        raise QueueError("ZOTERO_WRITE_CONFIRMATION_REQUIRED")


def _read_json(path, limit=_STATE_LIMIT):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise QueueError("INVALID_LOCAL_STATE")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise QueueError("INVALID_LOCAL_STATE") from exc
    if not isinstance(value, dict):
        raise QueueError("INVALID_LOCAL_STATE")
    return value


def _load_queue(download_dir):
    raw = Path(download_dir)
    if raw.is_symlink() or not raw.is_dir():
        raise QueueError("INVALID_DOWNLOAD_DIRECTORY")
    root = raw.resolve(strict=True)
    jobs_path, ledger_path = (root / ".paper-download-jobs.json",
                              root / ".paper-download-ledger.json")
    # lexists semantics for a dangling symlink: unsafe state is not first-use.
    present = [path.exists() or path.is_symlink() for path in (jobs_path, ledger_path)]
    if not any(present):
        return root, {}, False
    if not all(present):
        raise QueueError("INCOMPLETE_DOWNLOAD_STATE")
    jobs_state, ledger_state = _read_json(jobs_path), _read_json(ledger_path)
    for state, field in ((jobs_state, "jobs"), (ledger_state, "entries")):
        if type(state.get("version")) is not int or state["version"] != 1 or not isinstance(state.get(field), dict):
            raise QueueError("INVALID_DOWNLOAD_STATE")
    jobs = jobs_state["jobs"]
    if any(not isinstance(key, str) or not _JOB_KEY.fullmatch(key)
           or not isinstance(job, dict) for key, job in jobs.items()):
        raise QueueError("INVALID_DOWNLOAD_STATE")
    return root, jobs, True


def _candidates(jobs, job_keys):
    if job_keys is not None:
        if not isinstance(job_keys, (set, frozenset)) or any(
                not isinstance(key, str) or not _JOB_KEY.fullmatch(key) for key in job_keys):
            raise QueueError("INVALID_JOB_KEYS")
        invalid = sorted(key for key in job_keys
                         if key not in jobs or jobs[key].get("status") != "SAVED")
        if invalid:
            return [], [{"job_key": key, "reason_code": "JOB_NOT_SAVED"} for key in invalid]
        keys = list(job_keys)
    else:
        keys = [key for key, job in jobs.items() if job.get("status") == "SAVED"]
    if any(jobs[key].get("created_at") is not None
           and not isinstance(jobs[key]["created_at"], str) for key in keys):
        raise QueueError("INVALID_DOWNLOAD_STATE")
    return sorted(keys, key=lambda key: (jobs[key].get("created_at") or "", key)), []


def _metadata(mapping, candidates):
    if mapping is None:
        return {}
    if not isinstance(mapping, dict):
        raise QueueError("INVALID_METADATA")
    clean = {}
    for key, record in mapping.items():
        if key not in candidates or not isinstance(record, dict) or not set(record) <= _PUBLIC_METADATA:
            raise QueueError("INVALID_METADATA")
        for field, value in record.items():
            if field == "publication_type":
                if value not in ("journalArticle", "preprint"):
                    raise QueueError("INVALID_METADATA")
            elif field == "authors":
                if (not isinstance(value, list) or len(value) > 500 or any(
                        not _public_text(author, 500)
                        for author in value)):
                    raise QueueError("INVALID_METADATA")
            elif not _public_text(value, 2048 if field == "title" else 500):
                raise QueueError("INVALID_METADATA")
        if len(json.dumps(record, ensure_ascii=False).encode("utf-8")) > _METADATA_LIMIT:
            raise QueueError("INVALID_METADATA")
        clean[key] = {field: list(value) if field == "authors" else value
                      for field, value in record.items()}
    return clean


def _public_text(value, limit):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= limit
            and not any(ord(c) < 32 or ord(c) == 127 for c in value))


def _sidecar(root, job_key):
    """Read only public checkpoint fields; never return or serialize raw state."""
    try:
        state = _read_json(root / FILING_STATE)
        if (type(state.get("version")) is not int or state["version"] != 1
                or not isinstance(state.get("jobs"), dict)):
            return None
        row = state["jobs"].get(job_key)
        if not isinstance(row, dict):
            return None
        strict = (set(state) == {"version", "server_id", "jobs"}
                  and _RECORD_REQUIRED <= set(row)
                  and set(row) <= _RECORD_REQUIRED | _RECORD_AUDIT
                  and all(type(row[field]) is bool for field in
                          ("zotero_write_attempted", "zotero_write_uncertain") if field in row)
                  and ("updated_at" not in row or _valid_timestamp(row["updated_at"])))
        fields = _RECORD_REQUIRED | _RECORD_AUDIT
        return {"state_server_id": state.get("server_id"),
                "strict_record": strict, **{field: row[field] for field in fields if field in row}}
    except (QueueError, OSError, ValueError):
        return None


def _valid_timestamp(value):
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() is not None
    except ValueError:
        return False


def _flag(row, field):
    return isinstance(row, dict) and row.get(field) is True


def _filed_record_matches(record, answer, server_id):
    if not record or record.get("strict_record") is not True or record.get("status") != "FILED":
        return False
    if (not isinstance(server_id, str) or not server_id or len(server_id) > 1024
            or any(ord(c) < 33 or ord(c) > 126 for c in server_id)
            or record.get("state_server_id") != server_id or record.get("server_id") != server_id
            or record.get("zotero_write_uncertain", False) is not False):
        return False
    sha = record.get("source_pdf_sha256")
    if not isinstance(sha, str) or not _SHA.fullmatch(sha) or sha != answer.get("source_pdf_sha256"):
        return False
    return all(isinstance(record.get(field), str) and _OBJECT_KEY.fullmatch(record[field])
               and record[field] == answer.get(field) for field in _OBJECTS)


def _preview_eligible(before, after, answer, server_id):
    return (before == after and _filed_record_matches(before, answer, server_id)
            and all(answer.get(field) is False for field in _ACTIONS))


def _driver_result(answer, job_key, execute):
    expected = "FILED" if execute else "READY"
    if not isinstance(answer, dict) or answer.get("status") != expected or answer.get("job_key") != job_key:
        raise QueueError("FILING_FAILED")
    if (not isinstance(answer.get("source_pdf_sha256"), str)
            or not _SHA.fullmatch(answer["source_pdf_sha256"])
            or answer.get("mode") != ("execute" if execute else "dry-run")
            or any(type(answer.get(field)) is not bool for field in
                   (*_ACTIONS, "zotero_write_attempted", "zotero_write_uncertain"))):
        raise QueueError("FILING_FAILED")
    if execute and (answer["zotero_write_uncertain"] or answer.get("sidecar_written") is not True
                    or any(not isinstance(answer.get(field), str)
                           or not _OBJECT_KEY.fullmatch(answer[field]) for field in _OBJECTS)):
        raise QueueError("FILING_FAILED")
    if not execute and (answer["zotero_write_attempted"] or answer["zotero_write_uncertain"]
                        or answer.get("sidecar_written") is not False):
        raise QueueError("FILING_FAILED")
    return answer


def file_saved_queue(download_dir, *, maximum=1, execute=False, confirm_zotero_write=False,
                     client=None, storage_root=DEFAULT_STORAGE, metadata_by_job=None,
                     job_keys=None, candidate_lookup=None, collection=None,
                     halt_on_failure=False) -> dict:
    answer = _result(execute)
    try:
        _options(maximum, execute, confirm_zotero_write)
        if type(halt_on_failure) is not bool:
            raise QueueError("INVALID_OPTIONS")
        root, jobs, initialized = _load_queue(download_dir)
        answer["queue_initialized"] = initialized
        candidates, failures = _candidates(jobs, job_keys)
        if failures:
            answer["failures"] = failures
            return answer
        metadata = _metadata(metadata_by_job, candidates)
        selected = candidates[:maximum]
        answer["selected_job_keys"] = selected
        answer["deferred_by_limit"] = max(0, len(candidates) - maximum)
    except Exception as exc:
        answer["failures"].append({"job_key": None, "reason_code": _reason(exc)})
        return answer

    for job_key in selected:
        before = _sidecar(root, job_key)
        answer["prior_zotero_write_attempted"] |= _flag(before, "zotero_write_attempted")
        answer["prior_zotero_write_uncertain"] |= _flag(before, "zotero_write_uncertain")
        outcome = None
        try:
            if jobs[job_key].get("source") != "pubmed":
                raise QueueError("UNSUPPORTED_SOURCE")
            if client is None:
                client = ZoteroLocalClient()
            # Do not cache discovery, retry a failed job, or grant authority here.
            # The driver discovers/binds the server again for every job.
            outcome = _driver_result(file_saved_job(
                root, job_key, client=client, metadata=metadata.get(job_key),
                **({"collection": collection} if collection is not None else {}),
                storage_root=storage_root, execute=execute,
                **({"candidate_lookup": candidate_lookup} if candidate_lookup is not None else {})),
                job_key, execute)
            after = _sidecar(root, job_key)
            if execute:
                if not _filed_record_matches(after, outcome, client.server_id):
                    raise QueueError("FILING_STATE_READBACK_FAILED")
                answer["processed"].append(outcome)
                answer["eligible_job_keys"].append(job_key)
            else:
                answer["planned"].append(outcome)
                if _preview_eligible(before, after, outcome, client.server_id):
                    answer["eligible_job_keys"].append(job_key)
        except Exception as exc:
            answer["failures"].append({"job_key": job_key, "reason_code": _reason(exc)})
        finally:
            after = _sidecar(root, job_key)
            if outcome is not None and job_key in answer["eligible_job_keys"]:
                matched = (_filed_record_matches(after, outcome, client.server_id) if execute
                           else _preview_eligible(before, after, outcome, client.server_id))
                if not matched:
                    answer["eligible_job_keys"].remove(job_key)
                    if execute:
                        answer["processed"].remove(outcome)
                        answer["failures"].append({"job_key": job_key,
                                                   "reason_code": "FILING_STATE_READBACK_FAILED"})
            if execute:
                changed = after != before
                answer["zotero_write_attempted"] |= (
                    _flag(outcome, "zotero_write_attempted")
                    or (changed and _flag(after, "zotero_write_attempted")))
                # Current-cycle flags never inherit the prior checkpoint. A
                # newly submitted PARTIAL stays uncertain until FILED readback.
                if job_key not in answer["eligible_job_keys"]:
                    uncertain = (
                        (outcome is not None and outcome.get("status") == "FILED")
                        or _flag(outcome, "zotero_write_uncertain")
                        or (changed and (_flag(after, "zotero_write_uncertain")
                                         or (_flag(after, "zotero_write_attempted")
                                             and after.get("status") == "PARTIAL"))))
                    answer["zotero_write_uncertain"] |= uncertain
        if execute and halt_on_failure and (answer["failures"] or answer["zotero_write_uncertain"]):
            answer["paused_requires_review"] = True
            break
    return answer


def _cli_metadata(path):
    record = _read_json(path, _METADATA_LIMIT)
    if "metadata_source" in record:
        source = record.pop("metadata_source")
        if not _public_text(source, 2048):
            raise QueueError("INVALID_METADATA")
        try:
            url = urllib.parse.urlsplit(source)
        except ValueError as exc:
            raise QueueError("INVALID_METADATA") from exc
        if (url.scheme not in {"http", "https"} or not url.hostname
                or url.username is not None or url.password is not None or "#" in source):
            raise QueueError("INVALID_METADATA")
        if url.query:
            # Only the known public PubMed ESummary provenance query is allowed.
            # Every other parameter, including encoded credential names, fails.
            try:
                pairs = urllib.parse.parse_qsl(url.query, keep_blank_values=True,
                                              strict_parsing=True, max_num_fields=3)
            except ValueError as exc:
                raise QueueError("INVALID_METADATA") from exc
            query = dict(pairs)
            if (url.scheme != "https" or url.netloc.lower() != "eutils.ncbi.nlm.nih.gov"
                    or url.path != "/entrez/eutils/esummary.fcgi"
                    or len(pairs) != 3 or set(query) != {"db", "id", "retmode"}
                    or query["db"] != "pubmed" or query["retmode"] != "json"
                    or not re.fullmatch(r"[0-9]{1,80}", query["id"])):
                raise QueueError("INVALID_METADATA")
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download-dir", required=True, type=Path)
    parser.add_argument("--maximum", type=int, default=1)
    parser.add_argument("--job-key", action="append", help="repeat for a strict job subset")
    parser.add_argument("--metadata", type=Path, help="public bibliography JSON for a unique selected job")
    parser.add_argument("--storage-root", type=Path, default=DEFAULT_STORAGE)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-zotero-write", action="store_true")
    args = parser.parse_args(argv)
    keys = set(args.job_key) if args.job_key is not None else None
    metadata = None
    try:
        _options(args.maximum, args.execute, args.confirm_zotero_write)
        if args.metadata is not None:
            _, jobs, _ = _load_queue(args.download_dir)
            candidates, failures = _candidates(jobs, keys)
            if failures or len(candidates) != 1:
                raise QueueError("METADATA_REQUIRES_UNIQUE_JOB")
            metadata = {candidates[0]: _cli_metadata(args.metadata)}
        answer = file_saved_queue(args.download_dir, maximum=args.maximum, execute=args.execute,
                                  confirm_zotero_write=args.confirm_zotero_write,
                                  storage_root=args.storage_root, metadata_by_job=metadata, job_keys=keys)
    except Exception as exc:
        answer = _result(args.execute)
        answer["failures"].append({"job_key": None, "reason_code": _reason(exc)})
    print(json.dumps(answer, ensure_ascii=False, indent=2))
    return 2 if answer["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
