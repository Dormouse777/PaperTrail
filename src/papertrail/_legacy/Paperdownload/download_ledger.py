#!/usr/bin/env python3
"""Local PDF provenance and identifier-based duplicate detection.

The ledger only records verified local PDFs and publication identifiers. It
never stores credential-bearing PDF URLs. Existing unindexed files cannot be
assumed to match a paper merely because their filenames match.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


LEDGER_VERSION = 1
MIN_PDF_BYTES = 1024
DOI_PATTERN = re.compile(r"10\.\d{4,9}/\S+\Z", re.IGNORECASE)
PMCID_PATTERN = re.compile(r"PMC\d+\Z", re.IGNORECASE)
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class LedgerConflict(RuntimeError):
    """A saved identifier or PDF cannot be trusted without manual review."""


def normalized_doi(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    result = value.strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if result.lower().startswith(prefix):
            result = result[len(prefix):]
            break
    result = result.strip().lower()
    return result if DOI_PATTERN.fullmatch(result) else None


def identity_keys(paper: dict) -> list[str]:
    keys = []
    doi = normalized_doi(paper.get("doi"))
    if doi:
        keys.append(f"doi:{doi}")
    paper_id = str(paper.get("id") or "").strip()
    if paper.get("source") == "pubmed" and paper_id.isascii() and paper_id.isdigit():
        keys.append(f"pmid:{paper_id}")
    pmcid = str(paper.get("pmcid") or "").strip().upper()
    if PMCID_PATTERN.fullmatch(pmcid):
        keys.append(f"pmcid:{pmcid}")
    if paper.get("source") == "arxiv" and paper_id and len(paper_id) < 80:
        keys.append(f"arxiv:{paper_id.lower()}")
    return keys


def sanitized_source_page(url: object) -> str | None:
    if not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
    except ValueError:
        return None
    if (parsed.scheme not in {"http", "https"} or host not in
            {"pubmed.ncbi.nlm.nih.gov", "arxiv.org", "export.arxiv.org"}
            or parsed.username or parsed.password):
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def verify_pdf(path: Path) -> tuple[int, str]:
    if not path.is_file() or path.stat().st_size < MIN_PDF_BYTES:
        raise LedgerConflict("PDF is missing or smaller than 1 KiB")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise LedgerConflict("PDF header is invalid")
        stream.seek(0)
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


class DownloadLedger:
    def __init__(self, directory: Path):
        self.directory = Path(directory).resolve()
        self.path = self.directory / ".paper-download-ledger.json"
        if self.path.exists():
            try:
                self.state = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise LedgerConflict("Download ledger cannot be read") from exc
            if (not isinstance(self.state, dict) or self.state.get("version") != LEDGER_VERSION
                    or not isinstance(self.state.get("entries"), dict)):
                raise LedgerConflict("Download ledger version/format is unsupported")
        else:
            self.state = {"version": LEDGER_VERSION, "entries": {}}

    def _within_directory(self, filename: str) -> Path:
        if (not isinstance(filename, str) or not filename or filename in {".", ".."}
                or Path(filename).name != filename or not filename.lower().endswith(".pdf")):
            raise LedgerConflict("Ledger contains an unsafe PDF filename")
        path = self.directory / filename
        if path.resolve().parent != self.directory:
            raise LedgerConflict("Ledger PDF path escapes the download directory")
        return path

    def _verify_entry(self, entry: dict) -> tuple[Path, int, str]:
        if not isinstance(entry, dict):
            raise LedgerConflict("Ledger entry is malformed")
        path = self.source_path(entry)
        if not path.exists():
            recovery = entry.get("recovery", {})
            if (not isinstance(recovery, dict) or recovery.get("status") not in
                    {"CLEANUP_PENDING", "SOURCE_REMOVED", "ARCHIVED"}):
                raise LedgerConflict("PDF is missing without a verified recovery location")
            locations = recovery.get("locations", [])
            if not isinstance(locations, list):
                raise LedgerConflict("Recovery locations are malformed")
            for location in locations:
                if not isinstance(location, dict) or location.get("kind") not in {"zotero_stored", "archive"}:
                    continue
                alternate = Path(location.get("path", ""))
                if not alternate.is_absolute() or not alternate.is_file():
                    continue
                if location.get("kind") == "zotero_stored" and not all(
                        location.get(field) for field in ("server_id", "parent_key", "attachment_key")):
                    continue
                self._regular_chain(alternate)
                size, actual_hash = verify_pdf(alternate)
                if size != entry.get("size_bytes") or actual_hash != entry.get("sha256"):
                    raise LedgerConflict("Recovered PDF does not match the original content")
                return alternate, size, actual_hash
            raise LedgerConflict("No verified original is available; restore Zotero attachment or archive")
        size, actual_hash = verify_pdf(path)
        if entry.get("size_bytes") != size or entry.get("sha256") != actual_hash:
            raise LedgerConflict("Saved PDF no longer matches its verified ledger entry")
        return path, size, actual_hash

    @staticmethod
    def _regular_chain(path):
        for node in (path, *path.parents):
            info = node.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise LedgerConflict("PDF location contains a symlink or reparse point")

    def source_path(self, entry):
        filename = entry.get("filename")
        self._within_directory(filename)  # retain legacy basename validation
        relative = entry.get("relative_path", filename)
        if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise LedgerConflict("Unsafe paper bundle path")
        path = self.directory / relative
        if path.name != filename or not path.resolve().is_relative_to(self.directory):
            raise LedgerConflict("Paper bundle path escapes the download directory")
        if path.exists():
            self._regular_chain(path)
        return path

    def lookup(self, paper: dict) -> dict | None:
        hits = [self.state["entries"][key] for key in identity_keys(paper)
                if key in self.state["entries"]]
        if not hits:
            return None
        first = hits[0]
        if any(hit != first for hit in hits[1:]):
            raise LedgerConflict("Identifiers for this paper point to different saved PDFs")
        path, size, actual_hash = self._verify_entry(first)
        return {"pdf_path": str(path), "filename": first["filename"], "size_bytes": size, "sha256": actual_hash,
                "matched_identifiers": [key for key in identity_keys(paper) if key in self.state["entries"]]}

    def add_archive_location(self, paper: dict, archive: Path) -> None:
        """Register an existing byte-identical archive, without moving/deleting anything."""
        rows = [self.state["entries"][key] for key in identity_keys(paper)
                if key in self.state["entries"]]
        if not rows or any(row != rows[0] for row in rows):
            raise LedgerConflict("Archive registration needs one existing paper identity")
        entry = dict(rows[0])
        archive = Path(archive).absolute()
        self._regular_chain(archive)
        size, digest = verify_pdf(archive)
        if (size, digest) != (entry.get("size_bytes"), entry.get("sha256")):
            raise LedgerConflict("Archive content differs from the recorded original")
        recovery = dict(entry.get("recovery", {"status": "ARCHIVED", "locations": []}))
        locations = list(recovery.get("locations", []))
        location = {"kind": "archive", "path": str(archive)}
        if location not in locations:
            locations.append(location)
        recovery["locations"] = locations
        entry["recovery"] = recovery
        for key, row in list(self.state["entries"].items()):
            if row == rows[0]:
                self.state["entries"][key] = entry
        self._save()

    def record(self, paper: dict, path: Path) -> dict:
        path = Path(path).resolve()
        if not path.is_relative_to(self.directory):
            raise LedgerConflict("PDF is outside the download directory")
        size, actual_hash = verify_pdf(path)
        keys = identity_keys(paper)
        if not keys:
            keys = [f"sha256:{actual_hash}"]
        for key in keys:
            existing = self.state["entries"].get(key)
            if existing is not None:
                self._verify_entry(existing)
                if existing.get("sha256") != actual_hash or existing.get("filename") != path.name:
                    raise LedgerConflict(f"Publication identifier {key} already names another PDF")
        entry = {"filename": path.name, "size_bytes": size, "sha256": actual_hash,
                 "title": paper.get("title"), "source": paper.get("source"),
                 "source_id": paper.get("id"), "doi": normalized_doi(paper.get("doi")),
                 "pmcid": paper.get("pmcid"), "published": paper.get("published"),
                 "source_page": sanitized_source_page(paper.get("url")),
                 "verified_at": datetime.now(timezone.utc).isoformat()}
        prior = next((self.state["entries"][key] for key in keys if key in self.state["entries"]), None)
        entry["cleanup_policy"] = (prior.get("cleanup_policy", {"enabled": False}) if prior
                                   else {"enabled": True, "require_index": True})
        if path.parent != self.directory:
            entry["relative_path"] = path.relative_to(self.directory).as_posix()
        for key in keys:
            self.state["entries"][key] = entry
        self._save()
        return {"pdf_path": str(path), "size_bytes": size, "sha256": actual_hash,
                "matched_identifiers": keys}

    def _save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=self.directory,
                                             prefix=".paper-download-ledger.", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(self.state, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
