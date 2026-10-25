"""Wiley TDM PDF adapter using the verified API_URL and token header."""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import quote, urlsplit
import re
from uuid import UUID

from download_guard import GuardError
from .base import (AdapterFailure, PublisherAdapter, SourceSpec,
    DailyRequestBudgetError, _clean_public_reference, _validated_https_url,
    reserve_daily_request, host_is_public_literal_or_name, _normalize_doi)


class _SingleArticleTransport:
    """Consume at most one fixed API call and one validated anonymous hop."""

    def __init__(self, delegate, doi):
        self.delegate = delegate
        self.api_url = 'https://api.wiley.com/onlinelibrary/tdm/v1/articles/' + quote(doi, safe='')
        self.api_consumed = False
        self.binary_consumed = False
        self.binary_url = None

    def get(self, url, *, headers, timeout, allow_redirects=False):
        if allow_redirects is not False:
            raise ValueError('WILEY_SINGLE_ARTICLE_SCOPE_INVALID')
        if url == self.api_url:
            if self.api_consumed or set(headers) != {'Wiley-TDM-Client-Token', 'Accept'}:
                raise ValueError('WILEY_SINGLE_ARTICLE_SCOPE_INVALID')
            self.api_consumed = True
        else:
            if (not self.api_consumed or self.binary_consumed or url != self.binary_url
                    or headers != {'Accept': 'application/pdf'}):
                raise ValueError('WILEY_SINGLE_ARTICLE_SCOPE_INVALID')
            self.binary_consumed = True
            self.binary_url = None
        return self.delegate.get(url, headers=headers, timeout=timeout, allow_redirects=False)


class WileyAdapter(PublisherAdapter):
    adapter_ids = ("wiley",)
    source_id = "wiley-tdm-api"
    subscription = True
    api_domain = "api.wiley.com"
    credential_header = "Wiley-TDM-Client-Token"

    def __init__(self, registry_entry, transport=None, credential_reader=None,
                 *, api_directed_binary_doi=None):
        try:
            if api_directed_binary_doi is not None and type(api_directed_binary_doi) is not str:
                raise ValueError()
            bound_doi = (_normalize_doi(api_directed_binary_doi)
                         if api_directed_binary_doi is not None else None)
        except (TypeError, ValueError):
            raise ValueError('WILEY_SINGLE_ARTICLE_SCOPE_INVALID') from None
        super().__init__(registry_entry, transport, credential_reader)
        self.api_directed_binary_doi = bound_doi
        if bound_doi is not None:
            self.transport = _SingleArticleTransport(self.transport, bound_doi)

    @staticmethod
    def _validated_rate_limits(entry):
        limits = PublisherAdapter._validated_rate_limits(entry)
        # Wiley's official TDM guidance: <=60 requests/10 minutes, with at
        # least ten seconds between requests. Do not inherit the looser generic
        # five-second policy, even when a caller supplies an approved registry.
        if limits['interval_seconds'] < 10 or limits['per_minute'] > 6:
            raise AdapterFailure('manual', 'WILEY_RATE_POLICY_INVALID')
        return limits

    def _prepare_source(
        self,
        plan: Mapping[str, Any],
        paper: Any,
        doi: str,
        noncommercial_research: bool,
        permission_evidence: str | None,
    ) -> SourceSpec:
        if self.api_directed_binary_doi is not None and doi != self.api_directed_binary_doi:
            raise AdapterFailure('manual', 'WILEY_SINGLE_ARTICLE_SCOPE_MISMATCH')
        encoded = quote(doi, safe="")
        return SourceSpec(
            doi=doi,
            url=f"https://api.wiley.com/onlinelibrary/tdm/v1/articles/{encoded}",
            source=self.source_id,
            channel="wiley",
            license="publisher-permission",
            permission_evidence=permission_evidence,
        )

    def _request_headers(self, credential: str | None) -> dict[str, str]:
        if self.api_directed_binary_doi is not None:
            try:
                # Match Wiley's official client: normalize a UUID, not a URL,
                # registration key, bearer token or browser cookie. Never save it.
                credential = str(UUID(credential))
            except (ValueError, TypeError, AttributeError):
                raise AdapterFailure('AUTH_REQUIRED', 'WILEY_TOKEN_FORMAT_INVALID') from None
        return {"Wiley-TDM-Client-Token": credential or "", "Accept": "application/pdf"}

    def _validate_pdf_response(self, response):
        if self.api_directed_binary_doi is not None:
            mime = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
            if mime != 'application/pdf':
                raise AdapterFailure('INVALID_PDF', 'WILEY_PDF_CONTENT_TYPE_INVALID')

    def validated_binary_hosts(self):
        """Pure policy validation; no credential, HTTP, governor or writes."""
        policy = self.registry_entry.get("binary_redirect_policy")
        if (self.registry_entry.get("binary_redirect_verified") is not True
                or type(policy) is not dict
                or set(policy) != {"enabled", "target_hosts", "permission_evidence", "allow_signed_query", "max_hops"}
                or policy["enabled"] is not True or type(policy["max_hops"]) is not int
                or policy["max_hops"] != 1 or type(policy["allow_signed_query"]) is not bool
                or _clean_public_reference(policy["permission_evidence"]) is None
                or type(policy["target_hosts"]) is not list or not 1 <= len(policy["target_hosts"]) <= 8):
            raise AdapterFailure("manual", "WILEY_BINARY_POLICY_REQUIRED")
        hosts = set()
        for host in policy["target_hosts"]:
            if (type(host) is not str or host != host.lower() or host.endswith(".")
                    or len(host) > 253 or not re.fullmatch(r"[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?", host)
                    or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                           for label in host.split("."))
                    or not host_is_public_literal_or_name(host)):
                raise AdapterFailure("manual", "WILEY_BINARY_POLICY_INVALID")
            hosts.add(host)
        if len(hosts) != len(policy["target_hosts"]):
            raise AdapterFailure("manual", "WILEY_BINARY_POLICY_INVALID")
        return hosts

    def _follow_redirect(self, response, *, spec, directory, governor, limits):
        """One separately-approved binary hop, no token/cookie forwarding.

        Exact public host approval is independent of the initial API product.
        Location may be a signed capability: kept only in transport memory.
        """
        if self.api_directed_binary_doi is None:
            hosts = self.validated_binary_hosts()
            policy = self.registry_entry["binary_redirect_policy"]
        else:
            if (spec.doi != self.api_directed_binary_doi
                    or spec.url != self.transport.api_url
                    or self._response_status(response) not in {302, 303, 307, 308}):
                raise AdapterFailure('manual', 'WILEY_SINGLE_ARTICLE_SCOPE_MISMATCH')
            location = response.headers.get('Location')
            try:
                parsed = urlsplit(location) if type(location) is str else None
                host = parsed.hostname.lower() if parsed and parsed.hostname else ''
                if (not host or host.endswith('.') or not host_is_public_literal_or_name(host)
                        or len(host) > 253 or any(not re.fullmatch(
                            r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part) for part in host.split('.'))):
                    raise ValueError()
            except Exception:
                raise AdapterFailure('manual', 'WILEY_BINARY_LOCATION_REJECTED') from None
            hosts = {host}
            # The official API's capability is authorized for this one DOI/call,
            # not promoted to a persistent host allowlist or a second API token.
            policy = {'allow_signed_query': True}
        try:
            location = response.headers.get("Location")
            parsed = urlsplit(location) if type(location) is str else None
            if (parsed is None or '#' in location or any(ord(c) > 126 for c in location)
                    or re.search(r"%(?![0-9a-fA-F]{2})", location)
                    or _validated_https_url(location, allowed_hosts=hosts,
                        allow_query=policy["allow_signed_query"],
                        allowed_query=parsed.query if policy["allow_signed_query"] else None) is None):
                raise ValueError()
        except Exception:
            raise AdapterFailure("manual", "WILEY_BINARY_LOCATION_REJECTED") from None

        if self.api_directed_binary_doi is not None:
            self.transport.binary_url = location

        control_url = "https://" + parsed.hostname + "/"
        try:
            reserve_daily_request(directory, control_url, limits["per_day"])
            if parsed.hostname != self.api_domain:
                # One publisher-wide logical slot prevents cross-host hops
                # from bypassing Wiley's conservative ten-second policy.
                governor.acquire(spec.url, download_attempt=False)
            governor.acquire(control_url, download_attempt=True)
        except GuardError as failure:
            status = getattr(failure, "status", None)
            if status not in {"AUTH_EXPIRED", "ACCESS_DENIED", "DOMAIN_PAUSED", "RATE_LIMITED", "RETRY_WAIT"}:
                status = "RATE_LIMITED"
            status = "AUTH_REQUIRED" if status == "AUTH_EXPIRED" else status
            raise AdapterFailure(status, status) from None
        except DailyRequestBudgetError as failure:
            raise AdapterFailure("NEEDS_REVIEW", failure.code) from None
        except Exception:
            raise AdapterFailure("NEEDS_REVIEW", "WILEY_BINARY_GOVERNOR_FAILED") from None
        try:
            response.close()
        except Exception:
            raise AdapterFailure("NEEDS_REVIEW", "WILEY_REDIRECT_CLOSE_FAILED") from None
        try:
            binary = self.transport.get(location, headers={"Accept": "application/pdf"},
                                        timeout=30, allow_redirects=False)
        except Exception:
            raise AdapterFailure("FAILED", "WILEY_BINARY_TRANSPORT_ERROR") from None
        return binary, location

    def _http_error(self, governor, url, status, headers):
        # Pausing only a CDN would permit repeated authenticated API requests.
        # Mirror publisher errors to the initial API host as well, without
        # persisting the signed capability or inventing a successful retry.
        try:
            host = urlsplit(url).hostname
            control_url = "https://" + host + "/"
        except Exception:
            return "NEEDS_REVIEW"
        result = super()._http_error(governor, control_url, status, headers)
        if host != self.api_domain and (status in {401, 403, 406, 429} or 500 <= status <= 599):
            mirrored = super()._http_error(governor, "https://" + self.api_domain + "/", status, headers)
            if "NEEDS_REVIEW" in (result, mirrored):
                return "NEEDS_REVIEW"
        return result
