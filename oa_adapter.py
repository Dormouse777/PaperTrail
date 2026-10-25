"""OA PDF adapter for router-confirmed Unpaywall and validated PMC sources."""

from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import urlsplit

from pmc_open_data_metadata import PmcMetadataValidationError, validate_pmc_metadata

from .base import (
    AdapterFailure,
    PublisherAdapter,
    SourceSpec,
    _clean_public_reference,
    _validated_https_url,
)


_PMC_HOST = "pmc-oa-opendata.s3.amazonaws.com"
_CC_CODES = {"by", "by-sa", "by-nd", "by-nc", "by-nc-sa", "by-nc-nd"}
_MD5 = re.compile(r"[0-9a-f]{32}\Z", re.IGNORECASE)
_PMCID = re.compile(r"PMC[1-9][0-9]*\Z")
_PMC_LICENSE_URLS = {
    "CC0": "https://creativecommons.org/publicdomain/zero/1.0/",
    "CC BY": "https://creativecommons.org/licenses/by/4.0/",
    "CC BY-SA": "https://creativecommons.org/licenses/by-sa/4.0/",
    "CC BY-ND": "https://creativecommons.org/licenses/by-nd/4.0/",
    "CC BY-NC": "https://creativecommons.org/licenses/by-nc/4.0/",
    "CC BY-NC-SA": "https://creativecommons.org/licenses/by-nc-sa/4.0/",
    "CC BY-NC-ND": "https://creativecommons.org/licenses/by-nc-nd/4.0/",
}


def _cc_license(value: Any) -> str | None:
    if not isinstance(value, str) or value != value.strip():
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme.lower() != "https" or parsed.hostname != "creativecommons.org"
                or parsed.username is not None or parsed.password is not None
                or parsed.port is not None or parsed.query or parsed.fragment):
            return None
    except (TypeError, ValueError):
        return None
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 3 or any(not part or part in {".", ".."} for part in parts):
        return None
    if parts[0] == "licenses" and parts[1] in _CC_CODES:
        return value
    if parts[:2] == ["publicdomain", "zero"]:
        return value
    return None


def _pmc_license_code(value: Any) -> str | None:
    if isinstance(value, str) and value in _PMC_LICENSE_URLS:
        return value
    checked = _cc_license(value)
    if checked is None:
        return None
    return next((code for code, url in _PMC_LICENSE_URLS.items() if url == checked), None)


class OAAdapter(PublisherAdapter):
    adapter_ids = ("oa",)
    source_id = "oa"

    def _allowed_hosts(self) -> set[str]:
        entry = self.registry_entry or {}
        raw = entry.get("oa_pdf_domains", ())
        values = (raw,) if isinstance(raw, str) else raw
        if not isinstance(values, (list, tuple, set, frozenset)):
            return set()
        hosts: set[str] = set()
        for value in values:
            if not isinstance(value, str) or value != value.strip():
                continue
            candidate = value.lower().rstrip(".")
            if candidate and "/" not in candidate and "@" not in candidate and "?" not in candidate:
                hosts.add(candidate)
        return hosts

    def _oa_policy(self) -> tuple[str, str]:
        entry = self.registry_entry
        if not isinstance(entry, Mapping):
            raise AdapterFailure("manual", "OA_REGISTRY_ENTRY_INVALID")
        if (entry.get("license_status") != "file_level_required"
                or entry.get("endpoint_verified") is not True):
            raise AdapterFailure("manual", "OA_REGISTRY_POLICY_UNVERIFIED")
        evidence = None
        raw_evidence = entry.get("permission_evidence")
        evidence_values = (raw_evidence,) if isinstance(raw_evidence, str) else raw_evidence
        if isinstance(evidence_values, (list, tuple, set, frozenset)):
            for item in evidence_values:
                evidence = _clean_public_reference(item)
                if evidence is not None:
                    break
        if evidence is None:
            raise AdapterFailure("manual", "OA_PERMISSION_EVIDENCE_MISSING")
        if not self._allowed_hosts():
            raise AdapterFailure("manual", "OA_PDF_HOST_ALLOWLIST_EMPTY")
        return evidence, "file-level-license-required"

    def _prepare_source(
        self,
        plan: Mapping[str, Any],
        paper: Any,
        doi: str,
        noncommercial_research: bool,
        permission_evidence: str | None,
    ) -> SourceSpec:
        if noncommercial_research is not True:
            raise AdapterFailure("manual", "NONCOMMERCIAL_RESEARCH_REQUIRED")
        evidence, _ = self._oa_policy()
        channel = plan.get("channel")
        if channel is None:
            if plan.get("reason") == "verified_oa_pdf_license_and_host":
                channel = "unpaywall"
            elif plan.get("reason") in {
                "verified_pmc_oa_pdf_license",
                "verified_pmc_oa_pdf_license_and_host",
                "pmc_metadata_validated",
                "validated_pmc_oa_pdf",
            }:
                channel = "pmc"
        if paper is not None and not isinstance(paper, Mapping):
            raise AdapterFailure("manual", "PAPER_SHAPE_INVALID")

        if channel == "unpaywall":
            if (plan.get("reason") != "verified_oa_pdf_license_and_host"
                    or plan.get("oa_status") != "verified_oa"):
                raise AdapterFailure("manual", "UNPAYWALL_LOCATION_UNVERIFIED")
            url = plan.get("fulltext_url")
            license_value = plan.get("license")
            checked_license = _cc_license(license_value)
            if paper is not None:
                unpaywall = paper.get("unpaywall")
                if not isinstance(unpaywall, Mapping) and "is_oa" in paper:
                    unpaywall = paper
                location = unpaywall.get("best_oa_location") if isinstance(unpaywall, Mapping) else None
                if not isinstance(unpaywall, Mapping) or unpaywall.get("is_oa") is not True or not isinstance(location, Mapping):
                    raise AdapterFailure("manual", "UNPAYWALL_LOCATION_UNVERIFIED")
                location_url = location.get("url_for_pdf")
                location_license = _cc_license(location.get("license_url", location.get("license")))
                if (location_url != url or location_license is None
                        or location_license != checked_license):
                    raise AdapterFailure("manual", "UNPAYWALL_PDF_LICENSE_MISMATCH")
            if not isinstance(url, str) or checked_license is None:
                raise AdapterFailure("manual", "UNPAYWALL_PDF_LICENSE_MISMATCH")
            allowed = self._allowed_hosts()
            checked_url = _validated_https_url(url, allowed_hosts=allowed)
            if checked_url is None:
                raise AdapterFailure("manual", "OA_URL_REJECTED")
            return SourceSpec(
                doi=doi, url=checked_url, source="oa:unpaywall", channel="unpaywall",
                license=checked_license, permission_evidence=evidence,
            )

        if channel == "pmc":
            if plan.get("reason") != "validated_pmc_oa_pdf" or plan.get("oa_status") != "verified_oa":
                raise AdapterFailure("manual", "PMC_ROUTE_NOT_CONFIRMED")
            metadata = paper.get("pmc_metadata") if isinstance(paper, Mapping) else None
            if metadata is None and isinstance(paper, Mapping) and "pdf_url" in paper:
                metadata = paper

            if isinstance(metadata, Mapping):
                expected_pmid = paper.get("pubmedid", paper.get("pmid", plan.get("pmid")))
                if expected_pmid is None and paper.get("source") == "pubmed":
                    expected_pmid = paper.get("id")
                expected_pmcid = paper.get("pmcid", plan.get("pmcid", metadata.get("pmcid")))
                expected_title = paper.get("title", plan.get("title", metadata.get("title")))
                try:
                    checked = validate_pmc_metadata(
                        dict(metadata),
                        expected_pmid=expected_pmid if expected_pmid is not None else metadata.get("pmid"),
                        expected_pmcid=expected_pmcid,
                        expected_doi=doi,
                        expected_title=expected_title,
                        noncommercial_research=noncommercial_research,
                    )
                except (PmcMetadataValidationError, TypeError, ValueError, KeyError):
                    raise AdapterFailure("manual", "PMC_METADATA_INVALID") from None
                for field in ("pmid", "pmcid", "title", "version", "expected_pdf_md5"):
                    planned = plan.get(field)
                    checked_field = "expected_pdf_md5" if field == "expected_pdf_md5" else field
                    if planned is not None and planned != checked.get(checked_field):
                        raise AdapterFailure("manual", "PMC_PLAN_METADATA_MISMATCH")
                digest = checked.get("expected_pdf_md5")
                url = checked.get("anonymous_pdf_url")
                checked_license_code = str(checked.get("license_code"))
                plan_license = plan.get("license")
                if plan_license not in (checked_license_code, _PMC_LICENSE_URLS.get(checked_license_code)):
                    raise AdapterFailure("manual", "PMC_LICENSE_MISMATCH")
            else:
                # The router emits this minimal RoutePlan only after validating
                # all PMC metadata and flags. Recheck its identity, canonical
                # object path, exact checksum query, and explicit license here.
                pmcid = plan.get("pmcid")
                pmid = plan.get("pmid")
                version = plan.get("version")
                title = plan.get("title")
                digest = plan.get("expected_pdf_md5")
                url = plan.get("fulltext_url")
                checked_license_code = _pmc_license_code(plan.get("license"))
                if not isinstance(pmcid, str) or _PMCID.fullmatch(pmcid) is None:
                    raise AdapterFailure("manual", "PMC_PLAN_METADATA_INVALID")
                if (not isinstance(pmid, str) or not pmid.isascii() or not pmid.isdigit()
                        or not isinstance(title, str) or not title.strip() or len(title) > 2048
                        or any(ord(char) < 32 for char in title)
                        or type(version) is not int or not 1 <= version <= 100_000):
                    raise AdapterFailure("manual", "PMC_PLAN_METADATA_INVALID")
                if not isinstance(digest, str) or _MD5.fullmatch(digest) is None:
                    raise AdapterFailure("manual", "PMC_CHECKSUM_METADATA_INVALID")
                if checked_license_code is None:
                    raise AdapterFailure("manual", "PMC_LICENSE_MISMATCH")
                object_stem = f"{pmcid}.{version}"
                expected_path = f"/{object_stem}/{object_stem}.pdf"
                try:
                    parsed_plan_url = urlsplit(url) if isinstance(url, str) else None
                except (TypeError, ValueError):
                    parsed_plan_url = None
                if (parsed_plan_url is None or parsed_plan_url.path != expected_path
                        or parsed_plan_url.query != "md5=" + digest.lower()):
                    raise AdapterFailure("manual", "PMC_URL_MISMATCH")
                if paper is not None:
                    paper_pmid = paper.get("pubmedid", paper.get("pmid"))
                    if paper_pmid is None and paper.get("source") == "pubmed":
                        paper_pmid = paper.get("id")
                    paper_pmcid = paper.get("pmcid")
                    paper_title = paper.get("title")
                    if ((paper_pmid is not None and str(paper_pmid) != pmid)
                            or (paper_pmcid is not None and paper_pmcid != pmcid)
                            or (paper_title is not None and paper_title != title)):
                        raise AdapterFailure("manual", "PMC_PLAN_METADATA_MISMATCH")

            if not isinstance(digest, str) or _MD5.fullmatch(digest) is None:
                raise AdapterFailure("manual", "PMC_CHECKSUM_METADATA_INVALID")
            if plan.get("fulltext_url") is not None and plan.get("fulltext_url") != url:
                raise AdapterFailure("manual", "PMC_URL_MISMATCH")
            query = "md5=" + digest.lower()
            allowed = self._allowed_hosts()
            checked_url = _validated_https_url(
                url, allowed_hosts=allowed, allow_query=True, allowed_query=query
            )
            if checked_url is None or urlsplit(checked_url).hostname != _PMC_HOST:
                raise AdapterFailure("manual", "PMC_URL_REJECTED")
            return SourceSpec(
                doi=doi, url=checked_url, source="oa:pmc", channel="pmc",
                license=checked_license_code, permission_evidence=evidence,
                expected_md5=digest.lower(), allow_query=True, allowed_query=query,
            )

        raise AdapterFailure("manual", "OA_CHANNEL_NOT_CONFIRMED")

    def _request_headers(self, credential: str | None) -> dict[str, str]:
        return {"Accept": "application/pdf"}
