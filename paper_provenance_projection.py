"""Project verified SAVED PubMed job metadata into a small safe sidecar."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_PAPERDOWNLOAD_DIR = _SCRIPT_DIR.parents[1] / "Paperdownload"
for _import_dir in (_SCRIPT_DIR, _PAPERDOWNLOAD_DIR):
    _import_text = str(_import_dir)
    if _import_text not in sys.path:
        sys.path.insert(0, _import_text)

from download_ledger import DownloadLedger, identity_keys, normalized_doi  # noqa: E402
from handoff_saved_download import JOBS_FILE, _load_object, verify_saved_job  # noqa: E402


class ProvenanceProjectionError(ValueError):
    """A saved job or bibliography cannot be safely projected."""


_JOB_KEY = re.compile(r"pubmed:([0-9]+)\Z")
_HEX_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_ALLOWED_BIBLIOGRAPHY_FIELDS = {
    "title", "authors", "published", "journal", "doi", "pmcid",
    "pmid", "metadata_source",
}
_TEXT_LIMITS = {
    "title": 2048,
    "published": 64,
    "journal": 512,
    "doi": 255,
    "pmcid": 32,
    "pmid": 16,
    "metadata_source": 256,
}
_AUTHOR_COUNT_LIMIT = 128
_AUTHOR_LENGTH_LIMIT = 512
_BIBLIOGRAPHY_BYTES_LIMIT = 32 * 1024
_OUTPUT_SOURCE = "PubMed"

_INVALID_JOB = "Invalid PubMed saved job identity"
_VERIFY_FAILED = "Saved job or PDF verification failed"
_INVALID_BIBLIOGRAPHY = "Bibliography metadata is invalid"
_UNSUPPORTED_FIELDS = "Bibliography contains unsupported fields"
_UNBOUND_METADATA = "Bibliography metadata does not match the saved job"
_CONFLICTING_METADATA = "Bibliography metadata conflicts with the saved job"


def _fail(message: str) -> None:
    raise ProvenanceProjectionError(message) from None


def _has_control(value: str) -> bool:
    return any(ord(char) < 32 or 0x7F <= ord(char) < 0xA0 for char in value)


def _text(value: object, field: str, *, preserve_spacing: bool = False) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        _fail(_INVALID_BIBLIOGRAPHY)
    limit = _TEXT_LIMITS[field]
    if len(value) > limit or _has_control(value):
        _fail(_INVALID_BIBLIOGRAPHY)
    cleaned = value.strip() if preserve_spacing else " ".join(value.split())
    return cleaned or None


def _normalized_doi(value: str) -> str:
    # normalized_doi is the canonical project rule. Reject URL query/fragment
    # material first because that helper's DOI pattern accepts any non-space
    # suffix, including URL parameters.
    if "?" in value or "#" in value:
        _fail(_INVALID_BIBLIOGRAPHY)
    result = normalized_doi(value)
    if not result:
        _fail(_INVALID_BIBLIOGRAPHY)
    return result


def _normalized_pmcid(value: str) -> str:
    matches = [key[6:] for key in identity_keys({"pmcid": value})
               if key.startswith("pmcid:")]
    if len(matches) != 1:
        _fail(_INVALID_BIBLIOGRAPHY)
    return matches[0]


def _metadata_source(value: object) -> str | None:
    source = _text(value, "metadata_source")
    if source is None:
        return None
    # This field is a short source label, never a URL or a place to carry a
    # query, credential, or credential-bearing URL.
    unsafe = (
        re.search(r"(?i)(?:https?|ftp)://|www\.", source)
        or re.search(r"[/?#@\\]", source)
        or re.search(r"(?i)\b(?:token|session|auth|cookie|password|secret|api[_-]?key|query)\b", source)
        or re.search(r"\b(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b", source)
    )
    if unsafe:
        _fail(_INVALID_BIBLIOGRAPHY)
    return source


def _normalize_bibliography(value: object) -> dict:
    if value is None:
        return {}
    if type(value) is not dict:
        _fail(_INVALID_BIBLIOGRAPHY)
    if any(not isinstance(key, str) for key in value):
        _fail(_UNSUPPORTED_FIELDS)
    if set(value) - _ALLOWED_BIBLIOGRAPHY_FIELDS:
        _fail(_UNSUPPORTED_FIELDS)

    result: dict = {}
    for field in ("title", "journal"):
        cleaned = _text(value.get(field), field)
        if cleaned:
            result[field] = cleaned

    published = _text(value.get("published"), "published", preserve_spacing=True)
    if published:
        result["published"] = published

    raw_doi = _text(value.get("doi"), "doi")
    if raw_doi:
        result["doi"] = _normalized_doi(raw_doi)

    raw_pmcid = _text(value.get("pmcid"), "pmcid")
    if raw_pmcid:
        result["pmcid"] = _normalized_pmcid(raw_pmcid)

    pmid = _text(value.get("pmid"), "pmid")
    if pmid:
        if not pmid.isascii() or not pmid.isdigit():
            _fail(_INVALID_BIBLIOGRAPHY)
        result["pmid"] = pmid

    source = _metadata_source(value.get("metadata_source"))
    if source:
        result["metadata_source"] = source

    authors = value.get("authors")
    if authors is not None:
        if not isinstance(authors, list) or len(authors) > _AUTHOR_COUNT_LIMIT:
            _fail(_INVALID_BIBLIOGRAPHY)
        safe_authors = []
        for author in authors:
            if not isinstance(author, str) or len(author) > _AUTHOR_LENGTH_LIMIT or _has_control(author):
                _fail(_INVALID_BIBLIOGRAPHY)
            cleaned = " ".join(author.split())
            if cleaned:
                safe_authors.append(cleaned)
        if safe_authors:
            result["authors"] = safe_authors

    encoded_size = len(json.dumps(result, ensure_ascii=False,
                                  separators=(",", ":")).encode("utf-8"))
    if encoded_size > _BIBLIOGRAPHY_BYTES_LIMIT:
        _fail(_INVALID_BIBLIOGRAPHY)
    return result


def _job_identifier_sets(identifiers: object, source_id: str) -> tuple[set[str], set[str]]:
    if not isinstance(identifiers, list) or not identifiers:
        _fail(_INVALID_JOB)
    pmids: set[str] = set()
    dois: set[str] = set()
    pmcids: set[str] = set()
    for identifier in identifiers:
        if not isinstance(identifier, str):
            _fail(_INVALID_JOB)
        prefix, separator, raw_value = identifier.partition(":")
        if not separator:
            continue
        prefix = prefix.casefold()
        if prefix == "pmid":
            if not raw_value.isascii() or not raw_value.isdigit():
                _fail(_INVALID_JOB)
            pmids.add(raw_value)
        elif prefix == "doi":
            if (len(raw_value) > _TEXT_LIMITS["doi"] or _has_control(raw_value)
                    or "?" in raw_value or "#" in raw_value):
                _fail(_INVALID_JOB)
            value = normalized_doi(raw_value)
            if not value:
                _fail(_INVALID_JOB)
            dois.add(value)
        elif prefix == "pmcid":
            if len(raw_value) > _TEXT_LIMITS["pmcid"] or _has_control(raw_value):
                _fail(_INVALID_JOB)
            normalized = [key[6:] for key in identity_keys({"pmcid": raw_value})
                          if key.startswith("pmcid:")]
            if len(normalized) != 1:
                _fail(_INVALID_JOB)
            pmcids.add(normalized[0])

    if pmids != {source_id} or len(dois) > 1 or len(pmcids) > 1:
        _fail(_INVALID_JOB)
    return dois, pmcids


def _check_bibliography_binding(metadata: dict, source_id: str,
                                job_dois: set[str], job_pmcids: set[str]) -> None:
    if metadata.get("pmid") and metadata["pmid"] != source_id:
        _fail(_UNBOUND_METADATA)
    if metadata.get("doi") and metadata["doi"] not in job_dois:
        _fail(_UNBOUND_METADATA)
    if metadata.get("pmcid") and metadata["pmcid"] not in job_pmcids:
        _fail(_UNBOUND_METADATA)


def _read_saved_job(root: Path, job_key: str) -> dict:
    try:
        state = _load_object(root / JOBS_FILE, "download jobs")
        jobs = state.get("jobs")
        job = jobs.get(job_key) if isinstance(jobs, dict) else None
    except Exception:
        _fail(_VERIFY_FAILED)
    if not isinstance(job, dict):
        _fail(_VERIFY_FAILED)
    return job


def _recheck_verified_pdf(root: Path, job: dict, verified: dict) -> None:
    """Tie the re-read job record and current file back to verifier results."""
    try:
        filename = job.get("pdf_filename")
        expected_hash = job.get("pdf_sha256")
        expected_size = job.get("pdf_size_bytes")
        verified_hash = verified.get("pdf_sha256")
        verified_size = verified.get("pdf_size_bytes")
        verified_name = verified.get("pdf")
        if (
            not isinstance(filename, str)
            or Path(filename).name != filename
            or not isinstance(expected_hash, str)
            or not _HEX_SHA256.fullmatch(expected_hash)
            or type(expected_size) is not int
            or type(verified_size) is not int
            or not isinstance(verified_hash, str)
            or not _HEX_SHA256.fullmatch(verified_hash)
            or expected_hash.lower() != verified_hash.lower()
            or expected_size != verified_size
            or not isinstance(verified_name, str)
        ):
            _fail(_VERIFY_FAILED)

        # Resolve the same ledger-bound source (or verified recovery copy) used
        # by the verifier. New papers live in per-paper subdirectories.
        reader = DownloadLedger(root)
        rows = [reader.state['entries'][key] for key in job.get('identifiers', [])
                if key in reader.state['entries']]
        if (not rows or any(row != rows[0] for row in rows)
                or rows[0].get('filename') != filename
                or rows[0].get('sha256', '').lower() != expected_hash.lower()
                or rows[0].get('size_bytes') != expected_size):
            _fail(_VERIFY_FAILED)
        pdf = reader.source_path(rows[0])
        if not pdf.exists():
            pdf, _, _ = reader._verify_entry(rows[0])
        reader._regular_chain(pdf)
        verified_pdf = Path(verified_name)
        if (
            pdf.is_symlink()
            or not pdf.is_file()
            or verified_pdf != pdf
        ):
            _fail(_VERIFY_FAILED)

        digest = hashlib.sha256()
        with pdf.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                _fail(_VERIFY_FAILED)
            stream.seek(0)
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest().lower() != verified_hash.lower() or pdf.stat().st_size != verified_size:
            _fail(_VERIFY_FAILED)
    except ProvenanceProjectionError:
        raise
    except Exception:
        _fail(_VERIFY_FAILED)


def project_saved_paper_provenance(
    download_dir: Path,
    job_key: str,
    *,
    bibliography: dict | None = None,
) -> dict:
    """Return a strict public-metadata projection for one verified SAVED job.

    The function verifies the saved PDF and ledger via ``verify_saved_job``.
    It reads only the matching job record's bibliography after verification and
    never writes job, PDF, ledger, Zotero, or RAGFlow state.
    """
    if not isinstance(job_key, str) or not job_key.isascii():
        _fail(_INVALID_JOB)
    match = _JOB_KEY.fullmatch(job_key)
    if match is None:
        _fail(_INVALID_JOB)
    source_id = match.group(1)

    try:
        root = Path(download_dir).resolve(strict=True)
        verified = verify_saved_job(root, job_key)
    except Exception:
        _fail(_VERIFY_FAILED)

    job = _read_saved_job(root, job_key)
    if (
        job.get("status") != "SAVED"
        or job.get("source") != "pubmed"
        or job.get("source_id") != source_id
        or verified.get("source") != "pubmed"
        or verified.get("source_id") != source_id
        or job.get("identifiers") != verified.get("identifiers")
    ):
        _fail(_INVALID_JOB)

    _recheck_verified_pdf(root, job, verified)

    digest = verified.get("pdf_sha256")
    if not isinstance(digest, str) or not _HEX_SHA256.fullmatch(digest):
        _fail(_VERIFY_FAILED)

    job_dois, job_pmcids = _job_identifier_sets(job.get("identifiers"), source_id)
    job_bibliography = _normalize_bibliography(job.get("bibliography"))
    supplied_bibliography = _normalize_bibliography(bibliography)

    for field in ("doi", "pmcid", "published"):
        saved_value = job_bibliography.get(field)
        supplied_value = supplied_bibliography.get(field)
        if saved_value and supplied_value and saved_value != supplied_value:
            _fail(_CONFLICTING_METADATA)

    _check_bibliography_binding(job_bibliography, source_id, job_dois, job_pmcids)
    _check_bibliography_binding(supplied_bibliography, source_id, job_dois, job_pmcids)

    supplied_values = {key for key, value in supplied_bibliography.items() if value}
    has_supplementary_metadata = bool(supplied_values - {"pmid", "doi", "pmcid"})
    if has_supplementary_metadata and not (
        supplied_bibliography.get("pmid") == source_id
        or supplied_bibliography.get("doi") in job_dois
        or supplied_bibliography.get("pmcid") in job_pmcids
    ):
        _fail(_UNBOUND_METADATA)

    publication_date = job_bibliography.get("published") or supplied_bibliography.get("published")
    result = {
        "source_database": _OUTPUT_SOURCE,
        "source_id": source_id,
        "source_pdf_sha256": digest.lower(),
    }
    if job_dois:
        result["doi"] = next(iter(job_dois))
    if publication_date:
        result["publication_date"] = publication_date
        if job_bibliography.get("published"):
            date_source = job_bibliography.get("metadata_source") or _OUTPUT_SOURCE
        else:
            date_source = supplied_bibliography.get("metadata_source")
        result["publication_date_source"] = date_source
    return result
