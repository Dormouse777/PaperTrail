#!/usr/bin/env python3
"""Durable per-paper download status for safe restart and manual handoff.

This state does not contain PDF URLs, cookies, session tokens, or passwords.
It does not itself acquire or refresh an institutional session.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from download_ledger import (DownloadLedger, LedgerConflict, identity_keys,
                             normalized_doi)
from download_guard import safe_download_diagnostics
from pmc_open_data_metadata import validate_pmc_metadata
from zotero_preflight import normalize_pmid
from pdf_article_identity import (ARTICLE_IDENTITY_CODES, ArticleIdentityError, MAX_PDF_BYTES,
                                  compact, expected_identity, verify_article_identity)


JOB_VERSION = 1
MANUAL_STATES = {"AUTH_REQUIRED", "AUTH_EXPIRED", "ACCESS_DENIED", "DOMAIN_PAUSED",
                 "INVALID_PDF", "FAILED", "NEEDS_REVIEW"}
DEFERRED_STATES = {"RATE_LIMITED", "RETRY_WAIT"}
ALL_STATES = {"DISCOVERED", "AUTHENTICATED", "QUEUED", "FETCHING", "VERIFIED", "SAVED"} | MANUAL_STATES | DEFERRED_STATES

_BIBLIOGRAPHY_TEXT_LIMITS = {
    "title": 2048,
    "published": 64,
    "journal": 512,
    "doi": 255,
    "pmcid": 32,
}
_BIBLIOGRAPHY_AUTHORS_LIMIT = 128
_BIBLIOGRAPHY_AUTHOR_LENGTH_LIMIT = 512
_BIBLIOGRAPHY_BYTES_LIMIT = 32 * 1024


def _bounded_text(value: object, *, field: str, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    if len(value) > limit:
        raise ValueError(f"Bibliography {field} exceeds the safe size limit")
    text = " ".join(value.split())
    if not text:
        return None
    if any(ord(char) < 32 or 0x7F <= ord(char) < 0xA0 for char in text):
        raise ValueError(f"Bibliography {field} contains control characters")
    return text


def _safe_pmcid(value: object) -> str | None:
    text = _bounded_text(value, field="pmcid", limit=_BIBLIOGRAPHY_TEXT_LIMITS["pmcid"])
    if not text:
        return None
    candidate = {"pmcid": text}
    return next((key.removeprefix("pmcid:") for key in identity_keys(candidate)
                 if key.startswith("pmcid:")), None)


def _bibliography_from(value: object) -> dict:
    """Project only bounded, non-sensitive citation fields from a record."""
    if not isinstance(value, dict):
        return {}
    bibliography = {}
    for field, limit in _BIBLIOGRAPHY_TEXT_LIMITS.items():
        text = _bounded_text(value.get(field), field=field, limit=limit)
        if not text:
            continue
        if field == "doi":
            text = normalized_doi(text)
            if not text:
                continue
        elif field == "pmcid":
            text = _safe_pmcid(text)
            if not text:
                continue
        bibliography[field] = text

    authors = value.get("authors")
    if isinstance(authors, list):
        if len(authors) > _BIBLIOGRAPHY_AUTHORS_LIMIT:
            raise ValueError("Bibliography authors exceed the safe item-count limit")
        safe_authors = []
        for author in authors:
            text = _bounded_text(author, field="author",
                                 limit=_BIBLIOGRAPHY_AUTHOR_LENGTH_LIMIT)
            if text:
                safe_authors.append(text)
        if safe_authors:
            bibliography["authors"] = safe_authors

    size = len(json.dumps(bibliography, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8"))
    if size > _BIBLIOGRAPHY_BYTES_LIMIT:
        raise ValueError("Bibliography exceeds the safe total size limit")
    return bibliography


def _check_bibliography_identifiers(job: dict, old: dict, new: dict) -> None:
    identifiers = job.get("identifiers", [])
    if not isinstance(identifiers, list) or any(not isinstance(key, str) for key in identifiers):
        raise ValueError("Saved download job identifiers are malformed")
    existing = {"doi": set(), "pmcid": set()}
    for key in identifiers:
        if key.startswith("doi:"):
            value = normalized_doi(key[4:])
            if value:
                existing["doi"].add(value)
        elif key.lower().startswith("pmcid:"):
            value = _safe_pmcid(key.split(":", 1)[1])
            if value:
                existing["pmcid"].add(value)
    for field in existing:
        if old.get(field):
            existing[field].add(old[field])
        if len(existing[field]) > 1 or (
                new.get(field) and existing[field] and new[field] not in existing[field]):
            raise ValueError(f"Bibliography {field.upper()} conflicts with the saved job identity")


def _merged_bibliography(job: dict, paper: dict) -> dict:
    raw_old = job.get("bibliography", {})
    if not isinstance(raw_old, dict):
        raise ValueError("Saved download job bibliography is malformed")
    old = _bibliography_from(raw_old)
    new = _bibliography_from(paper)
    _check_bibliography_identifiers(job, old, new)
    merged = {**old, **new}
    size = len(json.dumps(merged, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8"))
    if size > _BIBLIOGRAPHY_BYTES_LIMIT:
        raise ValueError("Bibliography exceeds the safe total size limit")
    return merged


def job_key(paper: dict) -> str:
    source = paper.get("source")
    source_id = str(paper.get("id") or "").strip()
    if source == 'doi':
        doi = normalized_doi(source_id)
        if not doi or len(doi) > 255 or normalized_doi(paper.get('doi')) != doi:
            raise ValueError('DOI download job needs a matching normalized DOI')
        return 'doi:' + doi
    if source not in {"pubmed", "arxiv"} or not source_id or len(source_id) > 80:
        raise ValueError("Download job needs a stable PubMed or arXiv source ID")
    return f"{source}:{source_id.lower()}"


class DownloadJobStore:
    def __init__(self, directory: Path, *, clock=time.time):
        self.directory = Path(directory)
        self.path = self.directory / ".paper-download-jobs.json"
        self.clock = clock
        if self.path.exists():
            try:
                self.state = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ValueError("Download job state cannot be read") from exc
            if (not isinstance(self.state, dict) or self.state.get("version") != JOB_VERSION
                    or not isinstance(self.state.get("jobs"), dict)):
                raise ValueError("Download job state version/format is unsupported")
        else:
            self.state = {"version": JOB_VERSION, "jobs": {}}

    def _now(self) -> str:
        return datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()

    def _publisher_marked(self, filename, path=None) -> bool:
        if (not isinstance(filename, str) or Path(filename).name != filename
                or '/' in filename or '\\' in filename):
            return False
        path = Path(path) if path is not None else self.directory / filename
        return filename.startswith('doi-') or path.with_suffix('.provenance.json').exists()

    def _check_saved_title(self, job, paper, *, path=None) -> None:
        # Do not silently rewrite the identity of an already materialized
        # publisher file before the resume/consumer gates get to inspect it.
        if not self._publisher_marked(job.get('pdf_filename'), path):
            return
        incoming = paper.get('title')
        if incoming is not None:
            stored = job.get('title')
            if (not isinstance(stored, str) or not isinstance(incoming, str)
                    or not compact(stored) or compact(stored) != compact(incoming)):
                raise ArticleIdentityError('ARTICLE_TITLE_NOT_CONFIRMED')

    def validate_saved_article(self, key: str, paper: dict, known: dict) -> None:
        """Read-only publisher identity screen before SAVED or recovery.

        Unmarked historical/manual files retain their prior hash-only contract.
        Callers must obtain ``known`` from the real DownloadLedger.lookup, not
        a guessed filename or title. This does not establish full completeness.
        """
        job = self.state['jobs'][key]
        path = Path(known['pdf_path'])
        if not self._publisher_marked(known.get('filename', path.name), path):
            return
        if path.is_symlink():
            raise ArticleIdentityError('ARTICLE_PDF_UNREADABLE')
        if path.resolve().parent != self.directory.resolve():
            try:
                reader = DownloadLedger(self.directory)
                doi_key = 'doi:' + (normalized_doi(paper.get('doi')) or '')
                entry = reader.state['entries'].get(doi_key)
                if (not isinstance(entry, dict) or reader.source_path(entry).resolve() != path.resolve()
                        or entry.get('sha256') != known.get('sha256')
                        or entry.get('size_bytes') != known.get('size_bytes')):
                    raise ArticleIdentityError('ARTICLE_PDF_UNREADABLE')
            except (LedgerConflict, OSError):
                raise ArticleIdentityError('ARTICLE_PDF_UNREADABLE') from None
        bindings = {'pdf_filename': known.get('filename', path.name), 'pdf_sha256': known.get('sha256'),
                    'pdf_size_bytes': known.get('size_bytes')}
        if any(job.get(field) is not None and job[field] != value
               for field, value in bindings.items()):
            raise ArticleIdentityError('ARTICLE_PDF_CHANGED')
        self._check_saved_title({**job, 'pdf_filename': known.get('filename', path.name)}, paper, path=path)
        identifiers = job.get('identifiers')
        values = [value[4:] for value in identifiers
                  if isinstance(value, str) and value.startswith('doi:')] if isinstance(identifiers, list) else []
        if len(values) != 1:
            raise ArticleIdentityError('ARTICLE_DOI_INVALID')
        doi, title = expected_identity(values[0], job.get('title'))
        if paper.get('doi') is not None:
            incoming_doi, _ = expected_identity(paper['doi'], title)
            if doi != incoming_doi:
                raise ArticleIdentityError('ARTICLE_DOI_NOT_CONFIRMED')
        if type(known.get('size_bytes')) is not int or not 1024 <= known['size_bytes'] <= MAX_PDF_BYTES:
            raise ArticleIdentityError('ARTICLE_PDF_UNREADABLE')
        try:
            with path.open('rb') as stream:
                data = stream.read(MAX_PDF_BYTES + 1)
        except OSError:
            raise ArticleIdentityError('ARTICLE_PDF_UNREADABLE') from None
        if len(data) != known['size_bytes'] or hashlib.sha256(data).hexdigest() != known.get('sha256'):
            raise ArticleIdentityError('ARTICLE_PDF_CHANGED')
        verify_article_identity(data, doi=doi, title=title)

    def _pause_article(self, key: str, paper: dict, failure: ArticleIdentityError) -> None:
        self._transition(key, 'NEEDS_REVIEW', reason=failure.code)
        paper.update(status='NEEDS_REVIEW', reason_code=failure.code)

    def _transition(self, key: str, status: str, *, reason: str | None = None,
                    retry_at: float | None = None, domain: str | None = None,
                    diagnostics: dict | None = None) -> dict:
        if status not in ALL_STATES:
            raise ValueError(f"Unknown download status {status}")
        job = self.state["jobs"][key]
        job["status"] = status
        job["updated_at"] = self._now()
        job["retry_at"] = retry_at if isinstance(retry_at, (int, float)) else None
        job["failure_domain"] = domain
        job["reason_code"] = reason
        job["history"].append({"status": status, "at": job["updated_at"], "reason_code": reason})
        safe = safe_download_diagnostics(diagnostics)
        if safe:
            job['history'][-1]['download_diagnostics'] = safe
        job["history"] = job["history"][-100:]
        self._save()
        return job

    def ensure(self, paper: dict, query: str) -> str:
        key = job_key(paper)
        jobs = self.state["jobs"]
        if key not in jobs:
            bibliography = _bibliography_from(paper)
            now = self._now()
            jobs[key] = {"source": paper["source"], "source_id": str(paper["id"]),
                         "title": bibliography.get("title"), "query": query,
                         "identifiers": identity_keys(paper), "status": "DISCOVERED",
                         "bibliography": bibliography,
                         "attempts": 0, "created_at": now, "updated_at": now,
                         "retry_at": None, "failure_domain": None, "reason_code": None,
                         "pdf_filename": None, "pdf_sha256": None, "pdf_size_bytes": None,
                         "history": [{"status": "DISCOVERED", "at": now, "reason_code": None}]}
        else:
            job = jobs[key]
            if not isinstance(job, dict) or job.get("source") != paper["source"] or job.get("source_id") != str(paper["id"]):
                raise ValueError("Saved download job identity is inconsistent")
            self._check_saved_title(job, paper)
            bibliography = _merged_bibliography(job, paper)
            self._check_saved_title(job, {'title': bibliography.get('title')})
            identifiers = job.get("identifiers", [])
            if not isinstance(identifiers, list) or any(not isinstance(value, str) for value in identifiers):
                raise ValueError("Saved download job identifiers are malformed")
            job["title"] = bibliography.get("title") or job.get("title")
            job["identifiers"] = sorted(set(identifiers) | set(identity_keys(paper)))
            job["bibliography"] = bibliography
            job["query"] = query
        self._save()
        return key

    def resumable_keys(self, limit: int) -> list[str]:
        """Return due, unfinished jobs without clearing manual/auth pauses."""
        if limit < 1:
            raise ValueError("Resume limit must be positive")
        selected = []
        for key, job in sorted(self.state["jobs"].items(),
                               key=lambda pair: (pair[1].get("created_at", "")
                                                 if isinstance(pair[1], dict) else "", pair[0])):
            if not isinstance(job, dict):
                raise ValueError("Saved download job is malformed")
            status = job.get("status")
            if status not in ALL_STATES:
                raise ValueError("Saved download job has an unknown status")
            if status in MANUAL_STATES or status == "SAVED":
                continue
            if status in DEFERRED_STATES and job.get("retry_at") and self.clock() < job["retry_at"]:
                continue
            selected.append(key)
            if len(selected) >= limit:
                break
        return selected

    def prepare(self, key: str, paper: dict, ledger: DownloadLedger) -> bool:
        """Return True only when this job can enter FETCHING in this run."""
        job = self.state["jobs"][key]
        status = job["status"]
        if status in MANUAL_STATES:
            paper["status"] = status
            if isinstance(job.get('reason_code'), str) and job['reason_code'] in ARTICLE_IDENTITY_CODES:
                paper['reason_code'] = job['reason_code']
            return False
        if status in DEFERRED_STATES and job.get("retry_at") and self.clock() < job["retry_at"]:
            paper["status"] = status
            paper["retry_at"] = job["retry_at"]
            return False
        if status in {"SAVED", "FETCHING", "VERIFIED"}:
            try:
                known = ledger.lookup(paper)
            except LedgerConflict:
                self._transition(key, "NEEDS_REVIEW", reason="LEDGER_INTEGRITY")
                paper["status"] = "NEEDS_REVIEW"
                return False
            if known:
                try:
                    self.validate_saved_article(key, paper, known)
                except ArticleIdentityError as failure:
                    self._pause_article(key, paper, failure)
                    return False
                paper.update(known)
                paper["status"] = "exists"
                if status != "SAVED":
                    self._save_verified(key, known, reason="RECOVERED_AFTER_INTERRUPT")
                return False
            if status in {"SAVED", "VERIFIED"}:
                self._transition(key, "NEEDS_REVIEW", reason="SAVED_PDF_MISSING")
                paper["status"] = "NEEDS_REVIEW"
                return False
            self._transition(key, "QUEUED", reason="INTERRUPTED_FETCH_RETRY")
        elif status in {"DISCOVERED", "AUTHENTICATED"} | DEFERRED_STATES:
            self._transition(key, "QUEUED")
        if self.state["jobs"][key]["status"] != "QUEUED":
            raise ValueError(f"Unexpected download job state: {status}")
        return True

    def mark_fetching(self, key: str) -> None:
        job = self.state["jobs"][key]
        if job["status"] != "QUEUED":
            raise ValueError("Only a queued job can start fetching")
        job["attempts"] += 1
        self._transition(key, "FETCHING")

    def _save_verified(self, key: str, known: dict, *, reason: str | None = None) -> None:
        job = self._transition(key, "VERIFIED", reason=reason)
        job["pdf_filename"] = known.get("filename", Path(known["pdf_path"]).name)
        job["pdf_sha256"] = known["sha256"]
        job["pdf_size_bytes"] = known["size_bytes"]
        self._transition(key, "SAVED", reason=reason)

    def record_result(self, key: str, paper: dict, ledger: DownloadLedger) -> None:
        if self.state["jobs"][key]["status"] != "FETCHING":
            raise ValueError("Only a fetching job can record a result")
        status = paper.get("status")
        if status in {"downloaded", "exists"}:
            try:
                known = ledger.lookup(paper)
            except LedgerConflict:
                known = None
            if known is None:
                self._transition(key, "NEEDS_REVIEW", reason="LEDGER_INTEGRITY")
                return
            try:
                self.validate_saved_article(key, paper, known)
            except ArticleIdentityError as failure:
                self._pause_article(key, paper, failure)
                return
            self._save_verified(key, known)
            return
        mapped = status if status in ALL_STATES else "FAILED"
        self._transition(key, mapped, reason=str(status or "UNKNOWN"),
                         retry_at=paper.get("retry_at"), domain=paper.get("failure_domain"),
                         diagnostics=paper.get('download_diagnostics'))

    def mark_auth_required(self, key: str) -> None:
        self._transition(key, "AUTH_REQUIRED")

    def mark_authenticated(self, key: str) -> None:
        if self.state["jobs"][key]["status"] not in {"AUTH_REQUIRED", "AUTH_EXPIRED"}:
            raise ValueError("Job is not waiting for authentication")
        self._transition(key, "AUTHENTICATED", reason="USER_REAUTHENTICATED")

    def resume_after_review(self, key: str) -> None:
        if self.state["jobs"][key]["status"] not in MANUAL_STATES - {"AUTH_REQUIRED", "AUTH_EXPIRED"}:
            raise ValueError("Job is not paused for manual review")
        self._transition(key, "QUEUED", reason="USER_RESUMED")

    def approve_pmc_source_change(self, key: str, paper: dict, *, confirmed=False) -> dict:
        """Explicitly queue one denied Europe PMC job with a checked PMC OA source."""
        def reject() -> None:
            raise ValueError("PMC_SOURCE_REVIEW_REQUIRED") from None

        if confirmed is not True or not isinstance(key, str) or type(paper) is not dict:
            reject()
        jobs = self.state.get("jobs")
        job = jobs.get(key) if isinstance(jobs, dict) else None
        if (not isinstance(job, dict) or job.get("source") != "pubmed"
                or job.get("status") != "ACCESS_DENIED"
                or job.get("failure_domain") != "europepmc.org"
                or "source_review" in job):
            reject()

        history = job.get("history")
        if (type(history) is not list
                or not any(isinstance(row, dict) and row.get("status") == "ACCESS_DENIED"
                           for row in history)
                or type(job.get("attempts")) is not int or job["attempts"] < 1):
            reject()

        for record in (job, paper):
            if any("uncertain" in str(name).lower() and value is not None and value is not False
                   for name, value in record.items()):
                reject()
        if paper.get("status") is not None:
            reject()

        # Existing job fields are initialized to None. Any populated local-PDF
        # evidence blocks a source change, even if the status was edited.
        for field in ("pdf_filename", "pdf_sha256", "pdf_size_bytes", "pdf_path",
                      "sha256", "size_bytes"):
            if job.get(field) is not None or paper.get(field) is not None:
                reject()
        if any(record.get(field) is not None for record in (job, paper)
               for field in ("pdf", "pdf_bytes")):
            reject()

        try:
            if (paper.get("source") != "pubmed" or paper.get("pdf_source") != "pmc-oa-opendata"
                    or paper.get("open_access") is not True
                    or paper.get("pdf_available") is not True):
                reject()

            raw_bibliography = job.get("bibliography")
            if type(raw_bibliography) is not dict:
                reject()
            bibliography = _bibliography_from(raw_bibliography)
            if not all(bibliography.get(field) for field in ("doi", "pmcid", "title")):
                reject()

            pmid = normalize_pmid(job.get("source_id"))
            if (pmid is None or key != f"pubmed:{str(job.get('source_id')).lower()}"
                    or normalize_pmid(paper.get("id")) != pmid
                    or paper.get("id") != pmid
                    or paper.get("url") != f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"):
                reject()

            required_ids = set(identity_keys({
                "source": "pubmed", "id": pmid,
                "doi": bibliography["doi"], "pmcid": bibliography["pmcid"],
            }))
            identifiers = job.get("identifiers")
            if (type(identifiers) is not list
                    or any(type(value) is not str for value in identifiers)
                    or not required_ids.issubset(set(identifiers))):
                reject()
            stored_pmids = {
                normalize_pmid(value.split(":", 1)[1])
                for value in identifiers if value.lower().startswith("pmid:")
            }
            if stored_pmids != {pmid}:
                reject()
            _check_bibliography_identifiers(job, bibliography, _bibliography_from(paper))

            checked = validate_pmc_metadata(
                {
                    "pmid": paper.get("id"),
                    "pmcid": paper.get("pmcid"),
                    "doi": paper.get("doi"),
                    "title": paper.get("title"),
                    "version": paper.get("pdf_source_version"),
                    "is_pmc_openaccess": paper.get("open_access"),
                    # Retain the observed public flags, never invent a clean
                    # retraction/manuscript state when reviewing a candidate.
                    "is_manuscript": paper.get("is_manuscript"),
                    "is_retracted": paper.get("is_retracted"),
                    "license_code": paper.get("license_code"),
                    "pdf_url": paper.get("pdf_url"),
                },
                expected_pmid=pmid,
                expected_pmcid=bibliography["pmcid"],
                expected_doi=bibliography["doi"],
                expected_title=bibliography["title"],
                noncommercial_research=True,
            )
            digest = paper.get("expected_pdf_md5")
            if (paper.get("pdf_url") != checked["anonymous_pdf_url"]
                    or paper.get("pdf_source_version") != checked["version"]
                    or paper.get("license_code") != checked["license_code"]
                    or type(digest) is not str
                    or re.fullmatch(r"[0-9a-fA-F]{32}", digest) is None
                    or digest != checked["expected_pdf_md5"]):
                reject()

            ledger_path = self.directory / ".paper-download-ledger.json"
            if ledger_path.exists():
                if DownloadLedger(self.directory).lookup(paper) is not None:
                    reject()
        except (LedgerConflict, OSError, TypeError, ValueError, KeyError):
            reject()

        review = {
            "source": "pmc-oa-opendata",
            "version": checked["version"],
            "license_code": checked["license_code"],
            "md5": checked["expected_pdf_md5"],
            "previous_failure_domain": "europepmc.org",
            "previous_status": "ACCESS_DENIED",
            "review_time": self._now(),
        }
        job["source_review"] = review
        return self._transition(
            key, "QUEUED", reason="USER_APPROVED_PMC_SOURCE_CHANGE"
        )

    def _save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=self.directory,
                                             prefix=".paper-download-jobs.", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(self.state, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
