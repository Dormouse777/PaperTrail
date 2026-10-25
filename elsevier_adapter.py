"""Elsevier Article Retrieval API PDF adapter."""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import quote

from .base import AdapterFailure, PublisherAdapter, SourceSpec


class ElsevierAdapter(PublisherAdapter):
    adapter_ids = ("elsevier",)
    source_id = "elsevier-api"
    subscription = True
    api_domain = "api.elsevier.com"
    credential_header = "X-ELS-APIKey"

    def __init__(self, registry_entry, transport=None, credential_reader=None,
                 *, use_institution_token=False):
        if type(use_institution_token) is not bool:
            raise ValueError("ELSEVIER_AUTH_MODE_INVALID")
        super().__init__(registry_entry, transport, credential_reader)
        # Explicit opt-in, not discovery of ambient credentials. No auth API
        # call, token refresh, account choice, or fallback is performed here.
        self.use_institution_token = use_institution_token

    def _prepare_source(
        self,
        plan: Mapping[str, Any],
        paper: Any,
        doi: str,
        noncommercial_research: bool,
        permission_evidence: str | None,
    ) -> SourceSpec:
        encoded = quote(doi, safe="")
        query = "httpAccept=application/pdf&view=FULL"
        return SourceSpec(
            doi=doi,
            url=f"https://api.elsevier.com/content/article/doi/{encoded}?{query}",
            source=self.source_id,
            channel="elsevier",
            license="publisher-permission",
            permission_evidence=permission_evidence,
            allow_query=True,
            allowed_query=query,
        )

    def _request_headers(self, credential: str | None) -> dict[str, str]:
        headers = {"X-ELS-APIKey": credential or "", "Accept": "application/pdf"}
        if self.use_institution_token:
            token = self._read_credential("ELSEVIER_INSTTOKEN")
            if token is None or len(token) > 8192:
                raise AdapterFailure("AUTH_REQUIRED", "INSTITUTION_TOKEN_MISSING_OR_INVALID")
            headers["X-ELS-Insttoken"] = token
        return headers
