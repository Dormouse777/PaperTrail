"""Offline validation for one PMC Open Access PDF metadata record.

This module validates an already-obtained metadata mapping. It does not fetch
metadata, contact PMC, or download a PDF.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit

from zotero_preflight import normalize_pmcid, normalize_pmid, normalize_title
from zotero_preflight import normalized_doi


__all__ = ["PmcMetadataValidationError", "validate_pmc_metadata"]

_LICENSES = {
    "CC0": ("CC0", False),
    "CC-BY": ("CC BY", False),
    "CC-BY-SA": ("CC BY-SA", False),
    "CC-BY-ND": ("CC BY-ND", False),
    "CC-BY-NC": ("CC BY-NC", True),
    "CC-BY-NC-SA": ("CC BY-NC-SA", True),
    "CC-BY-NC-ND": ("CC BY-NC-ND", True),
}
_MD5_PATTERN = re.compile(r"[0-9a-fA-F]{32}\Z")
_MAX_TITLE_LENGTH = 2048
_MAX_VERSION = 100_000


class PmcMetadataValidationError(ValueError):
    """A fail-closed validation error with a stable, non-sensitive code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _license_record(value: object) -> tuple[str, bool] | None:
    if type(value) is not str:
        return None
    # The official values are short codes, not license URLs or prose. This
    # accepts case and separator spacing differences while keeping an exact
    # allowlist after normalization.
    token = re.sub(r"\s+", " ", value.strip().upper())
    if token.startswith("CC "):
        token = "CC-" + token[3:]
    return _LICENSES.get(token)


def _normalized_checked_title(value: object) -> str | None:
    if type(value) is not str or not value or len(value) > _MAX_TITLE_LENGTH:
        return None
    if any(unicodedata.category(char) == "Cc" for char in value):
        return None
    normalized = normalize_title(value)
    return normalized or None


def _validated_pdf_url(value: object, *, pmcid: str, version: int) -> tuple[str, str]:
    if type(value) is not str or not value:
        raise PmcMetadataValidationError("PDF_URL_INVALID")
    if any(ord(char) <= 0x20 or ord(char) >= 0x7F for char in value):
        raise PmcMetadataValidationError("PDF_URL_INVALID")
    if any(char in value for char in ("%", "\\", "#")):
        raise PmcMetadataValidationError("PDF_URL_INVALID")

    raw_scheme = value.partition(":")[0]
    if raw_scheme not in {"s3", "https"}:
        raise PmcMetadataValidationError("PDF_URL_INVALID")
    try:
        parsed = urlsplit(value)
        username = parsed.username
        password = parsed.password
        port = parsed.port
    except ValueError:
        raise PmcMetadataValidationError("PDF_URL_INVALID") from None

    allowed_authorities = {
        "s3": "pmc-oa-opendata",
        "https": "pmc-oa-opendata.s3.amazonaws.com",
    }
    if (
        parsed.scheme != raw_scheme
        or parsed.netloc != allowed_authorities[raw_scheme]
        or username is not None
        or password is not None
        or port is not None
        or parsed.fragment
    ):
        raise PmcMetadataValidationError("PDF_URL_INVALID")

    object_stem = f"{pmcid}.{version}"
    expected_path = f"/{object_stem}/{object_stem}.pdf"
    if parsed.path != expected_path:
        raise PmcMetadataValidationError("PDF_URL_INVALID")

    if not parsed.query.startswith("md5="):
        raise PmcMetadataValidationError("PDF_URL_INVALID")
    raw_digest = parsed.query[4:]
    if _MD5_PATTERN.fullmatch(raw_digest) is None:
        raise PmcMetadataValidationError("PDF_URL_INVALID")
    digest = raw_digest.lower()
    anonymous_url = (
        "https://pmc-oa-opendata.s3.amazonaws.com"
        f"{expected_path}?md5={digest}"
    )
    return anonymous_url, digest


def validate_pmc_metadata(
    metadata: object,
    *,
    expected_pmid: object,
    expected_pmcid: object,
    expected_doi: object,
    expected_title: object,
    noncommercial_research: bool = False,
) -> dict[str, object]:
    """Validate one official PMC OA metadata record without performing I/O.

    Identity values are compared with the same normalization helpers used by
    ``zotero_preflight``. The return value contains only the explicitly
    allowlisted, validated fields; arbitrary input fields are ignored.
    """
    if type(noncommercial_research) is not bool:
        raise PmcMetadataValidationError("NONCOMMERCIAL_FLAG_INVALID")
    if type(metadata) is not dict:
        raise PmcMetadataValidationError("METADATA_NOT_OBJECT")

    wanted_pmid = normalize_pmid(expected_pmid)
    wanted_pmcid = normalize_pmcid(expected_pmcid)
    wanted_doi = normalized_doi(expected_doi)
    wanted_title = _normalized_checked_title(expected_title)
    if not all((wanted_pmid, wanted_pmcid, wanted_doi)):
        raise PmcMetadataValidationError("EXPECTED_IDENTITY_INVALID")
    if wanted_title is None:
        raise PmcMetadataValidationError("EXPECTED_TITLE_INVALID")

    observed_pmid = normalize_pmid(metadata.get("pmid"))
    observed_pmcid = normalize_pmcid(metadata.get("pmcid"))
    observed_doi = normalized_doi(metadata.get("doi"))
    title = metadata.get("title")
    observed_title = _normalized_checked_title(title)
    if not all((observed_pmid, observed_pmcid, observed_doi)):
        raise PmcMetadataValidationError("METADATA_IDENTITY_INVALID")
    if observed_title is None:
        raise PmcMetadataValidationError("METADATA_TITLE_INVALID")
    if (
        observed_pmid != wanted_pmid
        or observed_pmcid != wanted_pmcid
        or observed_doi != wanted_doi
    ):
        raise PmcMetadataValidationError("IDENTITY_MISMATCH")
    if observed_title != wanted_title:
        raise PmcMetadataValidationError("TITLE_MISMATCH")

    version = metadata.get("version")
    if type(version) is not int or not 1 <= version <= _MAX_VERSION:
        raise PmcMetadataValidationError("VERSION_INVALID")

    is_open_access = metadata.get("is_pmc_openaccess")
    if type(is_open_access) is not bool:
        raise PmcMetadataValidationError("OPEN_ACCESS_FLAG_INVALID")
    if is_open_access is not True:
        raise PmcMetadataValidationError("NOT_OPEN_ACCESS")

    is_retracted = metadata.get("is_retracted")
    if type(is_retracted) is not bool:
        raise PmcMetadataValidationError("RETRACTION_FLAG_INVALID")
    if is_retracted is not False:
        raise PmcMetadataValidationError("RETRACTED_ARTICLE")

    is_manuscript = metadata.get("is_manuscript")
    if type(is_manuscript) is not bool:
        raise PmcMetadataValidationError("MANUSCRIPT_FLAG_INVALID")

    license_record = _license_record(metadata.get("license_code"))
    if license_record is None:
        raise PmcMetadataValidationError("LICENSE_UNSUPPORTED")
    license_code, requires_noncommercial = license_record
    if requires_noncommercial and noncommercial_research is not True:
        raise PmcMetadataValidationError("NONCOMMERCIAL_RESEARCH_REQUIRED")

    anonymous_pdf_url, expected_pdf_md5 = _validated_pdf_url(
        metadata.get("pdf_url"), pmcid=observed_pmcid, version=version
    )

    return {
        "pmcid": observed_pmcid,
        "pmid": observed_pmid,
        "doi": observed_doi,
        "title": title,
        "version": version,
        "is_pmc_openaccess": True,
        "is_manuscript": is_manuscript,
        "is_retracted": False,
        "license_code": license_code,
        "anonymous_pdf_url": anonymous_pdf_url,
        "expected_pdf_md5": expected_pdf_md5,
    }
