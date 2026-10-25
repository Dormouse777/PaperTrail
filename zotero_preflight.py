#!/usr/bin/env python3
"""Read-only duplicate preflight against Zotero's local API.

This module uses Zotero's supported local API only. It never opens Zotero's
SQLite database and exposes no write operation.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from download_ledger import identity_keys, normalized_doi


ZOTERO_ITEMS_URL = "http://127.0.0.1:23119/api/users/0/items"
PAGE_SIZE = 100
MAX_ITEMS = 250_000
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_RESPONSE_BYTES = 512 * 1024 * 1024
MAX_SCAN_SECONDS = 120.0
DEFAULT_REQUEST_TIMEOUT = 15.0
PMID_PATTERN = re.compile(r"\d+\Z")
PMCID_PATTERN = re.compile(r"PMC\d+\Z", re.IGNORECASE)
_EXTRA_ID_PATTERN = re.compile(
    r"(?:^|[\r\n;])\s*(PMID|PMCID)\s*:\s*([^\r\n;]+)", re.IGNORECASE
)


class ZoteroPreflightError(RuntimeError):
    """Base class for sanitized, fail-closed preflight errors."""

    code = "ZOTERO_API_ERROR"


class ZoteroUnavailableError(ZoteroPreflightError):
    code = "ZOTERO_UNAVAILABLE"


class ZoteroApiError(ZoteroPreflightError):
    code = "ZOTERO_API_ERROR"


class InvalidCandidateError(ValueError):
    """The proposed paper metadata cannot be safely compared."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def normalize_pmid(value: object) -> str | None:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    result = str(value).strip()
    if result.upper().startswith("PMID:"):
        result = result.split(":", 1)[1].strip()
    if not PMID_PATTERN.fullmatch(result):
        return None
    return result.lstrip("0") or "0"


def normalize_pmcid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    result = value.strip()
    if result.upper().startswith("PMCID:"):
        result = result.split(":", 1)[1].strip()
    if result.lower().startswith("https://") or result.lower().startswith("http://"):
        try:
            parsed = urllib.parse.urlsplit(result)
        except ValueError:
            return None
        if parsed.hostname not in {"pmc.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov"}:
            return None
        parts = [part for part in parsed.path.split("/") if part]
        result = next((part for part in parts if PMCID_PATTERN.fullmatch(part)), "")
    if not PMCID_PATTERN.fullmatch(result):
        return None
    return result.upper()


def normalize_title(value: object) -> str:
    """Casefold, use punctuation as separators, and retain Unicode letters/digits."""
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFKC", value).casefold()
    words: list[str] = []
    current: list[str] = []
    for char in text:
        if char.isalnum():
            current.append(char)
        elif current:
            words.append("".join(current))
            current = []
    if current:
        words.append("".join(current))
    return " ".join(words)


def _candidate_metadata(paper: dict[str, Any]) -> tuple[dict[str, Any], str]:
    if not isinstance(paper, dict):
        raise InvalidCandidateError("Paper metadata must be an object")

    source = paper.get("source")
    candidate: dict[str, Any] = {"source": "pubmed"}
    doi_value = paper.get("doi")
    if doi_value not in (None, ""):
        doi = normalized_doi(doi_value)
        if doi is None:
            raise InvalidCandidateError("DOI is malformed")
        candidate["doi"] = doi

    pmid_value = paper.get("pmid")
    if pmid_value in (None, "") and source == "pubmed":
        pmid_value = paper.get("id")
    if pmid_value not in (None, ""):
        pmid = normalize_pmid(pmid_value)
        if pmid is None:
            raise InvalidCandidateError("PMID is malformed")
        candidate["id"] = pmid

    pmcid_value = paper.get("pmcid")
    if pmcid_value not in (None, ""):
        pmcid = normalize_pmcid(pmcid_value)
        if pmcid is None:
            raise InvalidCandidateError("PMCID is malformed")
        candidate["pmcid"] = pmcid

    title = normalize_title(paper.get("title"))
    if not identity_keys(candidate) and not title:
        raise InvalidCandidateError("At least one valid publication identifier or title is required")
    return candidate, title


def _item_metadata(item: object) -> tuple[str, dict[str, Any]]:
    if not isinstance(item, dict):
        raise ZoteroApiError("Zotero returned a malformed item")
    data = item.get("data")
    if not isinstance(data, dict):
        raise ZoteroApiError("Zotero returned a malformed item")
    key = item.get("key") or data.get("key")
    item_type = data.get("itemType")
    if not isinstance(key, str) or not key or not isinstance(item_type, str) or not item_type:
        raise ZoteroApiError("Zotero returned a malformed item")
    return key, data


def _item_identifiers(data: dict[str, Any]) -> set[str]:
    identifiers: set[str] = set()
    doi = normalized_doi(data.get("DOI"))
    if doi:
        identifiers.add(f"doi:{doi}")

    pmid = normalize_pmid(data.get("PMID"))
    pmcid = normalize_pmcid(data.get("PMCID"))
    extra = data.get("extra")
    if isinstance(extra, str):
        for match in _EXTRA_ID_PATTERN.finditer(extra):
            label, value = match.group(1).upper(), match.group(2).strip()
            if label == "PMID" and pmid is None:
                pmid = normalize_pmid(value)
            elif label == "PMCID" and pmcid is None:
                pmcid = normalize_pmcid(value)
    if pmid:
        identifiers.add(f"pmid:{pmid}")
    if pmcid:
        identifiers.add(f"pmcid:{pmcid}")
    return identifiers


def _project_parent_item(item: object) -> tuple[str, dict[str, Any]] | None:
    """Keep only fields required for matching; discard all other Zotero data."""
    key, data = _item_metadata(item)
    item_type = data["itemType"]
    if item_type in {"attachment", "note", "annotation"}:
        return None
    title = data.get("title") if isinstance(data.get("title"), str) else ""
    identifiers = _item_identifiers(data)
    projected = {"key": key, "itemType": item_type, "title": title}
    for identifier in identifiers:
        field, value = identifier.split(":", 1)
        projected["DOI" if field == "doi" else field.upper()] = value
    return key, {"key": key, "data": projected}


def compare_with_zotero(paper: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare one paper to a completely fetched Zotero snapshot."""
    candidate, title_key = _candidate_metadata(paper)
    wanted_ids = set(identity_keys(candidate))
    found: dict[str, dict[str, Any]] = {}

    for item in items:
        key, data = _item_metadata(item)
        if data.get("itemType") in {"attachment", "note", "annotation"}:
            continue
        record = found.setdefault(key, {
            "key": key,
            "title": data.get("title") if isinstance(data.get("title"), str) else "",
            "item_type": data["itemType"],
            "reasons": set(),
        })
        if record["title"] != (data.get("title") if isinstance(data.get("title"), str) else ""):
            raise ZoteroApiError("Zotero pagination returned inconsistent item data")
        reasons: set[str] = record["reasons"]
        reasons.update(identifier.split(":", 1)[0] for identifier in wanted_ids & _item_identifiers(data))
        if title_key and title_key == normalize_title(data.get("title")):
            reasons.add("title")

    matches = [record for record in found.values() if record["reasons"]]
    matches.sort(key=lambda record: record["key"])
    id_matches = [record for record in matches if record["reasons"] - {"title"}]
    if len(matches) > 1:
        status, reason = "conflict", "MULTIPLE_ITEMS_CONFLICT"
    elif id_matches:
        status, reason = "duplicate", "IDENTIFIER_MATCH"
    elif matches:
        status, reason = "title_match", "TITLE_MATCH_REVIEW"
    else:
        status, reason = "clear", "NO_MATCH"

    public_matches = [{"key": record["key"], "title": record["title"],
                       "item_type": record["item_type"],
                       "match_reasons": sorted(record["reasons"])} for record in matches]
    return {
        "status": status,
        "reason_code": reason,
        "duplicate": status == "duplicate",
        "safe_to_continue": status == "clear",
        "checked_items": len(items),
        "matches": public_matches,
        "writes_performed": False,
    }


class ZoteroLocalApi:
    """Bounded reader for Zotero's loopback Local API v3 item endpoint."""

    def __init__(self, *, opener=None, timeout: float = DEFAULT_REQUEST_TIMEOUT,
                 page_size: int = PAGE_SIZE, max_items: int = MAX_ITEMS, clock=None):
        if not 1 <= page_size <= PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {PAGE_SIZE}")
        if timeout <= 0 or max_items < page_size:
            raise ValueError("Invalid Zotero API limits")
        self.timeout = timeout
        self.page_size = page_size
        self.max_items = max_items
        self.clock = clock or time.monotonic
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirectHandler()
        )

    def scan_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        start = 0
        pages = 0
        scanned_count = 0
        total_response_bytes = 0
        scan_started = self.clock()
        snapshot_version = None
        while True:
            elapsed = self.clock() - scan_started
            remaining = MAX_SCAN_SECONDS - elapsed
            if remaining <= 0:
                raise ZoteroApiError("Zotero scan exceeded the safe time limit")
            pages += 1
            if pages > (self.max_items // self.page_size) + 2:
                raise ZoteroApiError("Zotero pagination exceeded the safe scan limit")
            query = urllib.parse.urlencode({
                "format": "json", "limit": self.page_size, "start": start,
            })
            request = urllib.request.Request(
                f"{ZOTERO_ITEMS_URL}?{query}",
                headers={"Accept": "application/json", "Zotero-API-Version": "3"},
                method="GET",
            )
            try:
                with self.opener.open(request, timeout=min(self.timeout, remaining)) as response:
                    if response.getcode() != 200:
                        raise ZoteroApiError("Zotero Local API returned an unexpected status")
                    headers = getattr(response, "headers", None)
                    page_version = headers.get("Last-Modified-Version") if headers else None
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
            except ZoteroPreflightError:
                raise
            except urllib.error.HTTPError as exc:
                raise ZoteroApiError("Zotero Local API rejected the read request") from exc
            except urllib.error.URLError as exc:
                raise ZoteroUnavailableError("Zotero Local API is unavailable") from exc
            except (TimeoutError, OSError) as exc:
                raise ZoteroUnavailableError("Zotero Local API is unavailable") from exc
            except Exception as exc:  # noqa: BLE001 - never surface raw API/transport errors
                raise ZoteroApiError("Zotero Local API request failed") from exc

            if self.clock() - scan_started >= MAX_SCAN_SECONDS:
                raise ZoteroApiError("Zotero scan exceeded the safe time limit")

            if start == 0:
                snapshot_version = page_version
            elif not snapshot_version or page_version != snapshot_version:
                raise ZoteroApiError("Zotero changed or omitted its version during pagination")

            if len(raw) > MAX_RESPONSE_BYTES:
                raise ZoteroApiError("Zotero Local API page exceeded the response limit")
            total_response_bytes += len(raw)
            if total_response_bytes > MAX_TOTAL_RESPONSE_BYTES:
                raise ZoteroApiError("Zotero scan exceeded the total response limit")
            try:
                page = json.loads(raw.decode("utf-8"))
            except (UnicodeError, ValueError) as exc:
                raise ZoteroApiError("Zotero Local API returned invalid JSON") from exc
            if not isinstance(page, list) or len(page) > self.page_size:
                raise ZoteroApiError("Zotero Local API returned an invalid item page")

            for item in page:
                key, _ = _item_metadata(item)
                if key in seen_keys:
                    raise ZoteroApiError("Zotero pagination returned duplicate item keys")
                seen_keys.add(key)
                scanned_count += 1
                if scanned_count > self.max_items:
                    raise ZoteroApiError("Zotero library exceeds the safe scan limit")
                projected = _project_parent_item(item)
                if projected is not None:
                    items.append(projected[1])

            if len(page) < self.page_size:
                return items
            if not snapshot_version:
                raise ZoteroApiError("Zotero omitted its version during pagination")
            start += len(page)


def preflight_paper(paper: dict[str, Any], *, client: ZoteroLocalApi | None = None,
                    items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Return a safe decision; any incomplete scan raises and must block work."""
    if items is None:
        items = (client or ZoteroLocalApi()).scan_items()
    return compare_with_zotero(paper, items)
