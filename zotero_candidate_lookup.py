#!/usr/bin/env python3
"""Bounded, read-only Zotero searches for one paper candidate at a time."""

from __future__ import annotations

import urllib.parse
from typing import Any

from zotero_preflight import (
    InvalidCandidateError,
    ZoteroApiError,
    _candidate_metadata,
    _item_metadata,
    _project_parent_item,
    compare_with_zotero,
    normalize_pmid,
)


ITEMS_ROUTE = "/api/users/0/items"
PAGE_SIZE = 100
MAX_QUERY_RESULTS = 1000
MAX_UNIQUE_ITEMS = 1000
SUPPORTED_API_VERSION = "3"


class CandidateLookup:
    """Callable that returns preflight-projected items found by narrow queries.

    ``client`` must expose the read-only ``discover()`` and ``get_json(path)``
    methods of ZoteroLocalClient. No authorization or write method is used.
    """

    def __init__(self, client):
        self.client = client

    def __call__(self, paper: dict[str, Any]) -> list[dict[str, Any]]:
        _, _, queries = self._validated_queries(paper)
        self._discover()

        projected_by_key: dict[str, dict[str, Any]] = {}
        observed_by_key: dict[str, tuple[str, dict[str, Any] | None]] = {}
        for query in queries:
            self._collect_query(query, projected_by_key, observed_by_key)

        projected = list(projected_by_key.values())
        # Reuse the preflight comparator as a final shape and matching check.
        # A genuine multi-item match remains in the result so the caller can
        # report the comparator's conflict status instead of treating it as a
        # transport failure.
        try:
            compare_with_zotero(paper, projected)
        except Exception:
            raise ZoteroApiError("Zotero candidate results failed preflight validation") from None
        return projected

    @staticmethod
    def _validated_queries(paper: dict[str, Any]) -> tuple[dict[str, Any], str, list[str]]:
        try:
            candidate, title_key = _candidate_metadata(paper)
        except InvalidCandidateError:
            raise
        except Exception:
            raise InvalidCandidateError("Paper metadata is invalid") from None

        if not isinstance(paper, dict):
            raise InvalidCandidateError("Paper metadata must be an object")

        # _candidate_metadata accepts either pmid or PubMed's id. If both are
        # supplied, validate both and reject disagreement instead of silently
        # querying only one of them.
        pmid_values: set[str] = set()
        for value in (paper.get("pmid"),
                      paper.get("id") if paper.get("source") == "pubmed" else None):
            if value is None or value == "":
                continue
            normalized = normalize_pmid(value)
            if normalized is None:
                raise InvalidCandidateError("PMID is malformed")
            pmid_values.add(normalized)
        if len(pmid_values) > 1:
            raise InvalidCandidateError("Candidate PMID fields disagree")

        title_value = paper.get("title")
        if title_value is not None and title_value != "":
            if not isinstance(title_value, str) or not title_key:
                raise InvalidCandidateError("Title is malformed")

        queries: list[str] = []
        for field in ("doi", "id", "pmcid"):
            value = candidate.get(field)
            if isinstance(value, str) and value:
                queries.append(value)
        if title_key:
            queries.append(title_key)
        if not queries or any(not isinstance(query, str) or not query.strip() for query in queries):
            raise InvalidCandidateError("At least one non-empty candidate query is required")
        return candidate, title_key, queries

    def _discover(self) -> None:
        discover = getattr(self.client, "discover", None)
        get_json = getattr(self.client, "get_json", None)
        if not callable(discover) or not callable(get_json):
            raise ZoteroApiError("Zotero client must expose discover and get_json")
        try:
            result = discover()
            client_version = getattr(self.client, "api_version", None)
        except Exception:
            raise ZoteroApiError("Zotero discovery failed") from None
        if (not isinstance(result, dict)
                or result.get("available") is not True
                or result.get("api_version") != SUPPORTED_API_VERSION
                or client_version not in (None, SUPPORTED_API_VERSION)):
            raise ZoteroApiError("Unsupported or incomplete Zotero API discovery")

    def _collect_query(
        self,
        query: str,
        projected_by_key: dict[str, dict[str, Any]],
        observed_by_key: dict[str, tuple[str, dict[str, Any] | None]],
    ) -> None:
        query_keys: set[str] = set()
        start = 0
        while start < MAX_QUERY_RESULTS:
            params = urllib.parse.urlencode({
                "q": query,
                "qmode": "everything",
                "format": "json",
                "limit": PAGE_SIZE,
                "start": start,
            })
            try:
                page = self.client.get_json(f"{ITEMS_ROUTE}?{params}")
            except Exception:
                raise ZoteroApiError("Zotero candidate search failed") from None
            if not isinstance(page, list) or len(page) > PAGE_SIZE:
                raise ZoteroApiError("Zotero candidate search returned an invalid page")

            for raw_item in page:
                key, data = _item_metadata(raw_item)
                if key in query_keys:
                    raise ZoteroApiError("Zotero candidate pagination returned duplicate keys")
                query_keys.add(key)

                projected_item = _project_parent_item(raw_item)
                observed_item = (data["itemType"],
                                 projected_item[1] if projected_item is not None else None)
                if key in observed_by_key and observed_by_key[key] != observed_item:
                    raise ZoteroApiError("Zotero candidate item changed between searches")
                observed_by_key[key] = observed_item
                if projected_item is None:
                    continue
                projected_key, item = projected_item
                if projected_key != key:
                    raise ZoteroApiError("Zotero candidate item key is inconsistent")
                existing = projected_by_key.get(key)
                if existing is not None:
                    if existing != item:
                        raise ZoteroApiError("Zotero candidate item changed between searches")
                    continue
                projected_by_key[key] = item
                if len(projected_by_key) > MAX_UNIQUE_ITEMS:
                    raise ZoteroApiError("Zotero candidate search exceeded its safe result limit")

            if len(page) < PAGE_SIZE:
                return
            start += PAGE_SIZE

        # A full final page could mean the result set was truncated. Do not
        # return a partial candidate set as though it were complete.
        raise ZoteroApiError("Zotero candidate search exceeded its safe result limit")
