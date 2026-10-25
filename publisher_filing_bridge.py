"""Fail-closed bridge from one verified publisher PDF to the existing P5 queue.

Preview performs local checks only. Execution is deliberately not a client
factory: callers must inject the existing archive client, explicit public
metadata when needed, a one-item confirmation, and a lock shared with the
writer of the environment snapshot used to validate the download plan.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from download_ledger import DownloadLedger, normalized_doi, verify_pdf
from publishers.base import MAX_PDF_BYTES, _clean_public_reference
from pdf_article_identity import (ARTICLE_IDENTITY_CODES, ArticleIdentityError, compact,
                                  expected_identity, verify_article_identity)
from zotero_filing import FILING_STATE, FilingError, verified_job
from zotero_filing_queue import file_saved_queue


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PMID = re.compile(r"[0-9]{1,16}\Z")
_RESULT_FIELDS = {"status", "error", "publiccode", "pdf_path", "sha256", "source", "license"}
_BUNDLE_RESULT_FIELDS = {'bundle_path', 'paper_directory', 'supplements_status',
                         'supplements', 'supplements_error'}
_PROVENANCE_FIELDS = {
    "version", "doi", "source", "channel", "license", "permission_evidence",
    "sha256", "size_bytes",
}
_LOCAL_JSON_LIMIT = 64 * 1024 * 1024


class _BridgeFailure(Exception):
    """An internal fixed-code stop; exception details are never returned."""

    def __init__(self, code: str) -> None:
        self.code = code


def _public_text(value: Any, limit: int) -> bool:
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= limit
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _json_without_duplicates(text: str) -> Any:
    def pairs(items):
        answer = {}
        for key, value in items:
            if key in answer:
                raise ValueError("duplicate local JSON key")
            answer[key] = value
        return answer

    return json.loads(text, object_pairs_hook=pairs)


def _read_local_json(path: Path, limit: int = _LOCAL_JSON_LIMIT) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise _BridgeFailure("LOCAL_STATE_INVALID")
    try:
        value = _json_without_duplicates(path.read_text(encoding="utf-8"))
    except Exception:
        raise _BridgeFailure("LOCAL_STATE_INVALID") from None
    if not isinstance(value, dict):
        raise _BridgeFailure("LOCAL_STATE_INVALID")
    return value


def _target_archive_state_is_clear(root: Path, job_key: str) -> None:
    path = root / FILING_STATE
    if not path.exists() and not path.is_symlink():
        return
    state = _read_local_json(path)
    jobs = state.get("jobs")
    if (set(state) != {"version", "server_id", "jobs"}
            or type(state.get("version")) is not int or state["version"] != 1
            or not isinstance(state.get("server_id"), str)
            or not isinstance(jobs, dict)):
        raise _BridgeFailure("ARCHIVE_STATE_INVALID")
    # Existing FILED/PARTIAL/uncertain state is for a human or the P5 recovery
    # workflow; this bridge never blindly repeats it or edits that checkpoint.
    if job_key in jobs:
        raise _BridgeFailure("ARCHIVE_STATE_REQUIRES_REVIEW")


def _validate_one(download_dir: str | Path, result: Any, paper: Any) -> dict[str, Any]:
    if (not isinstance(result, Mapping) or not _RESULT_FIELDS.issubset(result)
            or not set(result) <= _RESULT_FIELDS | _BUNDLE_RESULT_FIELDS):
        raise _BridgeFailure("ADAPTER_RESULT_INVALID")
    if (result.get("status") not in {"downloaded", "exists"}
            or result.get("error") is not None or result.get("publiccode") is not None):
        raise _BridgeFailure("ADAPTER_RESULT_NOT_SUCCESSFUL")
    if not isinstance(paper, Mapping):
        raise _BridgeFailure("PAPER_IDENTITY_REQUIRED")

    pmid = paper.get("id")
    if paper.get("source") != "pubmed" or not isinstance(pmid, str) or not _PMID.fullmatch(pmid):
        raise _BridgeFailure("PUBMED_BINDING_REQUIRED")
    doi = normalized_doi(paper.get("doi"))
    if doi is None:
        raise _BridgeFailure("DOI_BINDING_REQUIRED")
    try:
        doi, title = expected_identity(doi, paper.get('title'))
    except ArticleIdentityError as failure:
        raise _BridgeFailure(failure.code) from None
    job_key = "pubmed:" + pmid

    raw_root = Path(download_dir)
    if raw_root.is_symlink() or not raw_root.is_dir():
        raise _BridgeFailure("DOWNLOAD_DIRECTORY_INVALID")
    try:
        root = raw_root.resolve(strict=True)
    except Exception:
        raise _BridgeFailure("DOWNLOAD_DIRECTORY_INVALID") from None

    raw_pdf = result.get("pdf_path")
    if not isinstance(raw_pdf, str) or not raw_pdf:
        raise _BridgeFailure("PDF_PATH_INVALID")
    pdf = Path(raw_pdf)
    if not pdf.is_absolute() or pdf.is_symlink():
        raise _BridgeFailure("PDF_PATH_INVALID")
    try:
        resolved_pdf = pdf.resolve(strict=True)
        resolved_parent = pdf.parent.resolve(strict=True)
    except Exception:
        raise _BridgeFailure("PDF_MISSING") from None
    if (not resolved_pdf.is_relative_to(root) or resolved_parent != resolved_pdf.parent
            or resolved_pdf.name != pdf.name or not resolved_pdf.is_file()
            or pdf.suffix.lower() != ".pdf"):
        raise _BridgeFailure("PDF_PATH_OUTSIDE_DOWNLOAD_DIRECTORY")
    for entry in (pdf, *pdf.parents):
        if entry.is_symlink() or (entry.exists() and getattr(entry.stat(), 'st_file_attributes', 0) & 0x400):
            raise _BridgeFailure("PDF_PATH_OUTSIDE_DOWNLOAD_DIRECTORY")
        if entry == root:
            break

    declared_sha = result.get("sha256")
    source = result.get("source")
    license_value = result.get("license")
    if (not isinstance(declared_sha, str) or not _SHA256.fullmatch(declared_sha)
            or not _public_text(source, 128) or not _public_text(license_value, 1024)):
        raise _BridgeFailure("ADAPTER_RESULT_INVALID")
    try:
        size, actual_sha = verify_pdf(resolved_pdf)
        if size > MAX_PDF_BYTES:
            raise _BridgeFailure("PDF_SIZE_INVALID")
        data = resolved_pdf.read_bytes()
        if len(data) != size or hashlib.sha256(data).hexdigest() != actual_sha:
            raise _BridgeFailure('PDF_CHANGED_DURING_VALIDATION')
        if not data.rstrip().endswith(b"%%EOF"):
            raise _BridgeFailure("PDF_STRUCTURE_INVALID")
        from pypdf import PdfReader

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            reader = PdfReader(io.BytesIO(data), strict=True)
            if reader.is_encrypted or len(reader.pages) < 1:
                raise _BridgeFailure("PDF_STRUCTURE_INVALID")
    except _BridgeFailure:
        raise
    except Exception:
        raise _BridgeFailure("PDF_STRUCTURE_INVALID") from None
    if actual_sha != declared_sha:
        raise _BridgeFailure("RESULT_PDF_HASH_MISMATCH")

    provenance_path = resolved_pdf.with_suffix(".provenance.json")
    provenance = _read_local_json(provenance_path, 64 * 1024)
    evidence = provenance.get("permission_evidence")
    safe_evidence = _clean_public_reference(evidence)
    if (set(provenance) != _PROVENANCE_FIELDS
            or type(provenance.get("version")) is not int or provenance["version"] != 1
            or normalized_doi(provenance.get("doi")) != doi
            or provenance.get("source") != source
            or provenance.get("license") != license_value
            or provenance.get("sha256") != actual_sha
            or type(provenance.get("size_bytes")) is not int
            or provenance.get("size_bytes") != size
            or not _public_text(provenance.get("channel"), 64)
            or not isinstance(evidence, str) or not evidence.strip()
            or safe_evidence is None or safe_evidence != evidence):
        raise _BridgeFailure("PROVENANCE_BINDING_INVALID")

    identity = {"source": "pubmed", "id": pmid, "doi": doi}
    try:
        ledger_match = DownloadLedger(root).lookup(identity)
    except Exception:
        raise _BridgeFailure("DOWNLOAD_LEDGER_INVALID") from None
    required_identifiers = {"doi:" + doi, "pmid:" + pmid}
    if (not isinstance(ledger_match, dict)
            or not required_identifiers.issubset(set(ledger_match.get("matched_identifiers", [])))
            or ledger_match.get("sha256") != actual_sha
            or Path(str(ledger_match.get("pdf_path", ""))).resolve() != resolved_pdf):
        raise _BridgeFailure("DOWNLOAD_LEDGER_BINDING_MISMATCH")

    try:
        verified_root, job, candidate, saved_pdf, saved_identity = verified_job(root, job_key)
    except FilingError as failure:
        code = str(failure)
        raise _BridgeFailure(code if code in ARTICLE_IDENTITY_CODES else 'SAVED_JOB_INVALID') from None
    except Exception:
        raise _BridgeFailure("SAVED_JOB_INVALID") from None
    if (verified_root != root or candidate.get("source") != "pubmed"
            or candidate.get("id") != pmid or normalized_doi(candidate.get("doi")) != doi
            or saved_pdf.resolve() != resolved_pdf or saved_identity.get("sha256") != actual_sha
            or saved_identity.get("size") != size):
        raise _BridgeFailure("SAVED_JOB_BINDING_MISMATCH")
    identifiers = job.get("identifiers")
    if (not isinstance(identifiers, list)
            or "pmid:" + pmid not in identifiers or "doi:" + doi not in identifiers):
        raise _BridgeFailure("SAVED_JOB_BINDING_MISMATCH")
    if not isinstance(candidate.get('title'), str) or compact(candidate['title']) != compact(title):
        raise _BridgeFailure('SAVED_JOB_TITLE_MISMATCH')
    try:
        # Legacy SAVED/hash/provenance records are not proof of body identity.
        # _validate_one is also called again under snapshot_lock before P5.
        verify_article_identity(data, doi=doi, title=title)
    except ArticleIdentityError as failure:
        raise _BridgeFailure(failure.code) from None
    _target_archive_state_is_clear(root, job_key)

    return {
        "root": root,
        "pdf_path": resolved_pdf,
        "sha256": actual_sha,
        "size_bytes": size,
        "doi": doi,
        "pmid": pmid,
        "job_key": job_key,
        "source": source,
        "license": license_value,
    }


def _answer(status: str, mode: str, code: str | None = None, *, binding=None,
            write_attempted: bool = False, write_uncertain: bool = False,
            filing=None) -> dict[str, Any]:
    answer = {
        "status": status,
        "mode": mode,
        "reason_code": code,
        "filed": status == "FILED",
        "zotero_write_attempted": write_attempted is True,
        "zotero_write_uncertain": write_uncertain is True,
    }
    if binding is not None:
        answer.update({
            "job_key": binding["job_key"],
            "doi": binding["doi"],
            "pmid": binding["pmid"],
            "pdf_path": str(binding["pdf_path"]),
            "sha256": binding["sha256"],
            "source": binding["source"],
            "license": binding["license"],
        })
    if isinstance(filing, dict):
        # FILED retains its existing main-PDF contract. Surface the independent
        # bundle result so a failed supplement is never reported as complete.
        answer['pdf_status'] = 'FILED' if status == 'FILED' else filing.get('pdf_status', 'not_checked')
        supplement_status = filing.get('supplements_status', 'not_checked')
        if supplement_status not in {'complete', 'partial', 'not_checked', 'not_applicable', 'planned'}:
            supplement_status = 'partial'
        answer['supplements_status'] = supplement_status
        answer['bundle_complete'] = status == 'FILED' and supplement_status == 'complete'
        fields = {'status', 'relative_path', 'filename', 'zotero_filename', 'content_type',
                  'size_bytes', 'sha256', 'md5', 'attachment_key', 'reason_code'}
        if isinstance(filing.get('supplement_attachments'), list):
            answer['supplement_attachments'] = [{k: v for k, v in row.items() if k in fields}
                for row in filing['supplement_attachments'] if isinstance(row, dict)]
    return answer


def bridge_publisher_result(
    download_dir: str | Path,
    result: Any,
    paper: Any,
    *,
    license_confirmed: bool = False,
    execute: bool = False,
    confirm_single_item_write: bool = False,
    archive_client: Any = None,
    result_snapshot_sha256: str | None = None,
    snapshot_reader: Callable[[], str] | None = None,
    snapshot_lock: Any = None,
    storage_root: str | Path | None = None,
    collection: str | None = None,
    public_metadata: dict | None = None,
) -> dict[str, Any]:
    """Validate one adapter result and optionally pass one SAVED PubMed job to P5.

    ``result_snapshot_sha256`` must be the environment/registry snapshot against
    which the caller validated this result. For execution, ``snapshot_lock``
    must be shared with snapshot writers and held through the archive operation;
    ``snapshot_reader`` must return the current digest without network access.
    The bridge never constructs an archive client, reads credentials, downloads,
    searches metadata, renames files, or changes the publisher download ledger.
    """
    mode = "execute" if execute is True else "preview"
    if type(execute) is not bool or type(confirm_single_item_write) is not bool:
        return _answer("manual", mode, "EXECUTION_FLAGS_INVALID")
    if type(license_confirmed) is not bool:
        return _answer("manual", mode, "LICENSE_CONFIRMATION_INVALID")
    if execute is False and confirm_single_item_write:
        return _answer("manual", mode, "EXECUTION_FLAGS_INVALID")

    try:
        binding = _validate_one(download_dir, result, paper)
    except _BridgeFailure as failure:
        return _answer("manual", mode, failure.code)
    except Exception:
        return _answer("NEEDS_REVIEW", mode, "LOCAL_VALIDATION_FAILED")

    if license_confirmed is not True:
        return _answer("manual", mode, "LICENSE_UNCONFIRMED", binding=binding)
    if execute is False:
        return _answer("READY", "preview", binding=binding)

    if confirm_single_item_write is not True:
        return _answer("manual", mode, "SINGLE_ITEM_CONFIRMATION_REQUIRED", binding=binding)
    if archive_client is None:
        return _answer("manual", mode, "ARCHIVE_CLIENT_REQUIRED", binding=binding)
    if (not isinstance(result_snapshot_sha256, str)
            or not _SHA256.fullmatch(result_snapshot_sha256)
            or not callable(snapshot_reader)
            or not hasattr(snapshot_lock, "__enter__")
            or not hasattr(snapshot_lock, "__exit__")):
        return _answer("manual", mode, "ENVIRONMENT_SNAPSHOT_GUARD_REQUIRED", binding=binding)
    if storage_root is None:
        return _answer("manual", mode, "STORAGE_ROOT_REQUIRED", binding=binding)
    if not _public_text(collection, 150):
        return _answer("manual", mode, "COLLECTION_REQUIRED", binding=binding)
    if public_metadata is not None and not isinstance(public_metadata, dict):
        return _answer("manual", mode, "PUBLIC_METADATA_INVALID", binding=binding)

    try:
        storage = Path(storage_root)
        if storage.is_symlink() or not storage.is_dir():
            return _answer("manual", mode, "STORAGE_ROOT_INVALID", binding=binding)
        storage = storage.resolve(strict=True)
    except Exception:
        return _answer("manual", mode, "STORAGE_ROOT_INVALID", binding=binding)

    archive_call_started = False
    try:
        with snapshot_lock:
            try:
                current_snapshot = snapshot_reader()
            except Exception:
                return _answer("NEEDS_REVIEW", mode, "ENVIRONMENT_SNAPSHOT_UNAVAILABLE", binding=binding)
            if current_snapshot != result_snapshot_sha256:
                return _answer("manual", mode, "ENVIRONMENT_SNAPSHOT_CHANGED", binding=binding)

            # Recheck all local bindings immediately before invoking the existing
            # P5 gate; no adapter status flag can bypass this second check.
            fresh = _validate_one(download_dir, result, paper)
            if fresh["sha256"] != binding["sha256"] or fresh["job_key"] != binding["job_key"]:
                return _answer("NEEDS_REVIEW", mode, "LOCAL_BINDING_CHANGED", binding=binding)

            metadata_by_job = None
            if public_metadata is not None:
                metadata_by_job = {binding["job_key"]: public_metadata}
            # From this point the queue may have performed or checkpointed a
            # mutation even if it raises before returning a structured answer.
            archive_call_started = True
            queue_answer = file_saved_queue(
                binding["root"], maximum=1, execute=True,
                confirm_zotero_write=True, client=archive_client,
                storage_root=storage, metadata_by_job=metadata_by_job,
                job_keys={binding["job_key"]}, collection=collection,
            )
            try:
                final_snapshot = snapshot_reader()
            except Exception:
                return _answer("NEEDS_REVIEW", mode, "ENVIRONMENT_SNAPSHOT_UNAVAILABLE",
                               binding=binding,
                               write_attempted=queue_answer.get("zotero_write_attempted") is True,
                               write_uncertain=queue_answer.get("zotero_write_uncertain") is True)
            if final_snapshot != result_snapshot_sha256:
                return _answer("NEEDS_REVIEW", mode, "ENVIRONMENT_SNAPSHOT_CHANGED",
                               binding=binding,
                               write_attempted=queue_answer.get("zotero_write_attempted") is True,
                               write_uncertain=queue_answer.get("zotero_write_uncertain") is True)

            processed = queue_answer.get("processed")
            exact_filed = (
                isinstance(queue_answer, dict)
                and queue_answer.get("mode") == "execute"
                and queue_answer.get("selected_job_keys") == [binding["job_key"]]
                and queue_answer.get("eligible_job_keys") == [binding["job_key"]]
                and queue_answer.get("failures") == []
                and queue_answer.get("zotero_write_uncertain") is False
                and isinstance(processed, list) and len(processed) == 1
                and isinstance(processed[0], dict)
                and processed[0].get("status") == "FILED"
                and processed[0].get("job_key") == binding["job_key"]
                and processed[0].get("source_pdf_sha256") == binding["sha256"]
                and processed[0].get("sidecar_written") is True
                and processed[0].get("zotero_write_uncertain") is False
            )
            if not exact_filed:
                return _answer("NEEDS_REVIEW", mode, "P5_FILING_NOT_VERIFIED",
                               binding=binding,
                               write_attempted=queue_answer.get("zotero_write_attempted") is True,
                               write_uncertain=queue_answer.get("zotero_write_uncertain") is True)
            return _answer("FILED", mode, binding=binding,
                           write_attempted=queue_answer.get("zotero_write_attempted") is True,
                           write_uncertain=False, filing=processed[0])
    except _BridgeFailure as failure:
        return _answer("manual", mode, failure.code, binding=binding)
    except Exception:
        # The archive queue may have persisted a partial checkpoint. Never
        # retry, delete, or include exception text; hand it to human review.
        return _answer("NEEDS_REVIEW", mode, "P5_FILING_NEEDS_REVIEW", binding=binding,
                       write_attempted=archive_call_started,
                       write_uncertain=archive_call_started)

