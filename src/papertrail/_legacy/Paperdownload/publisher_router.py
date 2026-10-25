"""Offline, conservative DOI routing for publisher adapters.

This module never performs HTTP, reads environment variables, downloads files,
or writes state. Public-metadata lookup is opt-in with ``resolve_metadata=True``
and goes only through the injected client. Source order is PMC, Unpaywall,
then Crossref; a source error is terminal for that DOI. Crossref license links
and text-mining links never prove the license of a particular PDF.

Metadata client contract (one call per reached source, no router retries):

    client.get_pmc(doi, *, preview) -> validated-source mapping | None  # optional
    client.get_unpaywall(doi, *, preview) -> mapping | None
    client.get_crossref(doi, *, preview) -> mapping | None

PMC results must include the fields returned by
``pmc_open_data_metadata.validate_pmc_metadata`` plus ``pdf_url`` equal to
``anonymous_pdf_url``; the router revalidates this mapping and compares its
checked URL, MD5, DOI, license, and identity fields. Unpaywall's single
``best_oa_location`` is authoritative for OA routing.

Credential callback contract (used only when ``preview=False``):

    credential_present(environment_variable_name) -> bool

It receives a variable *name*, never a credential value. Registry approval,
public permission evidence, and a verified endpoint remain independent gates.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
import re
from typing import Any, Callable, Literal, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

from pmc_open_data_metadata import validate_pmc_metadata


Action = Literal["download", "manual", "skip"]

_DOI_RE = re.compile(r"^10\.\d{4,9}/[A-Z0-9._;()/:+\-]+$", re.IGNORECASE)
_PREFIX_RE = re.compile(r"^10\.\d{4,9}(?:/[A-Z0-9._;()/:+\-]+)?$", re.IGNORECASE)
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

_CC_CODES = frozenset({"by", "by-sa", "by-nd", "by-nc", "by-nc-sa", "by-nc-nd"})
_NC_CODES = frozenset({"by-nc", "by-nc-sa", "by-nc-nd"})
_LICENSE_URLS = {
    "cc0": "https://creativecommons.org/publicdomain/zero/1.0/",
    "cc-by": "https://creativecommons.org/licenses/by/4.0/",
    "cc-by-sa": "https://creativecommons.org/licenses/by-sa/4.0/",
    "cc-by-nd": "https://creativecommons.org/licenses/by-nd/4.0/",
    "cc-by-nc": "https://creativecommons.org/licenses/by-nc/4.0/",
    "cc-by-nc-sa": "https://creativecommons.org/licenses/by-nc-sa/4.0/",
    "cc-by-nc-nd": "https://creativecommons.org/licenses/by-nc-nd/4.0/",
}
_PMC_LICENSE_TO_CODE = {
    "CC0": "cc0",
    "CC BY": "cc-by",
    "CC BY-SA": "cc-by-sa",
    "CC BY-ND": "cc-by-nd",
    "CC BY-NC": "cc-by-nc",
    "CC BY-NC-SA": "cc-by-nc-sa",
    "CC BY-NC-ND": "cc-by-nc-nd",
}

# Exact public host for PMC OA cloud objects. No endpoint is constructed here;
# its md5 query is accepted only after PMC metadata validation.
OFFICIAL_PMC_CLOUD_DOMAINS = frozenset({"pmc-oa-opendata.s3.amazonaws.com"})


@dataclass(frozen=True, slots=True)
class RoutePlan:
    """A safe, metadata-minimal routing decision for one input DOI."""

    doi: str | None
    publisher: str | None
    doi_prefix: str | None
    license: str | None
    oa_status: str
    adapter: str | None
    action: Action
    reason: str
    fulltext_url: str | None
    pmid: str | None = None
    pmcid: str | None = None
    title: str | None = None
    version: int | None = None
    expected_pdf_md5: str | None = None


class MetadataClient(Protocol):
    def get_crossref(self, doi: str, *, preview: bool) -> Mapping[str, Any] | None:
        """Return a Crossref response/message."""

    def get_unpaywall(self, doi: str, *, preview: bool) -> Mapping[str, Any] | None:
        """Return an Unpaywall response."""


class PmcMetadataClient(Protocol):
    def get_pmc(self, doi: str, *, preview: bool) -> Mapping[str, Any] | None:
        """Return a checked PMC source mapping, or None when absent."""


def normalize_doi(value: str) -> str:
    """Return a lowercase DOI or raise ``ValueError('invalid_doi')``.

    Accepts a bare DOI, ``doi:`` form, or an HTTP(S) doi.org/dx.doi.org URL.
    It deliberately does not guess at trailing citation punctuation or decode
    arbitrary URL escapes.
    """

    if not isinstance(value, str):
        raise ValueError("invalid_doi")
    candidate = value.strip()
    if not candidate or any(ord(char) < 0x21 or ord(char) > 0x7E for char in candidate):
        raise ValueError("invalid_doi")

    if candidate.lower().startswith(("https://", "http://")):
        try:
            parsed = urlsplit(candidate)
            hostname = (parsed.hostname or "").lower()
            if (parsed.scheme.lower() not in {"https", "http"}
                    or hostname not in {"doi.org", "dx.doi.org"}
                    or parsed.username is not None or parsed.password is not None
                    or parsed.port is not None or "?" in candidate or "#" in candidate
                    or parsed.query or parsed.fragment):
                raise ValueError("invalid_doi")
            candidate = parsed.path.lstrip("/")
        except (ValueError, TypeError):
            raise ValueError("invalid_doi") from None
    elif candidate[:4].lower() == "doi:":
        candidate = candidate[4:].strip()

    if not _DOI_RE.fullmatch(candidate):
        raise ValueError("invalid_doi")
    return candidate.lower()


def _registry_entries(registry: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    entries: list[tuple[str, Mapping[str, Any]]] = []
    for key, value in registry.items():
        if key in {"oa", "oa_pdf_domains"}:
            continue
        if isinstance(key, str) and isinstance(value, Mapping):
            entries.append((key, value))
    return entries


def _normalise_prefix(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().rstrip("/")
    if _PREFIX_RE.fullmatch(candidate):
        return candidate.lower()
    return None


def _publisher_by_prefix(
    doi: str, entries: Sequence[tuple[str, Mapping[str, Any]]]
) -> tuple[str | None, str | None, str | None]:
    matches: list[tuple[int, str, str]] = []
    for key, entry in entries:
        raw_prefixes = entry.get("doi_prefixes", ())
        prefixes = [raw_prefixes] if isinstance(raw_prefixes, str) else raw_prefixes
        if not isinstance(prefixes, (list, tuple, set, frozenset)):
            continue
        for raw_prefix in prefixes:
            prefix = _normalise_prefix(raw_prefix)
            if prefix and (doi == prefix or doi.startswith(prefix + "/")):
                matches.append((len(prefix), key, prefix))
    if not matches:
        return None, None, None
    longest = max(item[0] for item in matches)
    winners = {(key, prefix) for size, key, prefix in matches if size == longest}
    if len(winners) != 1:
        return None, None, "publisher_prefix_ambiguous"
    publisher, prefix = next(iter(winners))
    return publisher, prefix, None


def _normalise_name(value: str) -> str:
    return "".join(char.lower() for char in value if char.isalnum())


def _publisher_by_name(
    publisher_name: Any, entries: Sequence[tuple[str, Mapping[str, Any]]]
) -> tuple[str | None, str | None]:
    if not isinstance(publisher_name, str) or not publisher_name.strip():
        return None, "publisher_unrecognized"
    requested = _normalise_name(publisher_name)
    matches: set[str] = set()
    for key, entry in entries:
        aliases = entry.get("publisher_names", ())
        aliases = [aliases] if isinstance(aliases, str) else aliases
        if not isinstance(aliases, (list, tuple, set, frozenset)):
            aliases = ()
        candidates = [key, *[alias for alias in aliases if isinstance(alias, str)]]
        if any(_normalise_name(candidate) == requested for candidate in candidates):
            matches.add(key)
    if len(matches) == 1:
        return next(iter(matches)), None
    if len(matches) > 1:
        return None, "publisher_name_ambiguous"
    return None, "publisher_unrecognized"


def _crossref_fields(metadata: Mapping[str, Any]) -> Mapping[str, Any]:
    value = metadata.get("crossref", metadata)
    if not isinstance(value, Mapping):
        return {}
    message = value.get("message")
    return message if isinstance(message, Mapping) else value


def _normalise_host(value: Any) -> str | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    candidate = value.lower()
    if candidate.endswith(".") or any(char in candidate for char in "/@?#\\"):
        return None
    try:
        candidate = candidate.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if len(candidate) > 253:
        return None
    try:
        ip_address(candidate)
        return candidate
    except ValueError:
        pass
    labels = candidate.split(".")
    if len(labels) < 2 or any(not _HOST_LABEL_RE.fullmatch(label) for label in labels):
        return None
    if candidate in {"localhost", "metadata.google.internal"}:
        return None
    if candidate.endswith((".localhost", ".local", ".internal", ".test", ".invalid", ".onion")):
        return None
    return candidate


def _public_host(host: str) -> bool:
    try:
        address = ip_address(host)
    except ValueError:
        return True
    return address.is_global


def _allowed_oa_hosts(registry: Mapping[str, Any]) -> set[str]:
    raw_hosts: list[Any] = []
    oa_entry = registry.get("oa")
    if isinstance(oa_entry, Mapping):
        configured = oa_entry.get("oa_pdf_domains", ())
        if isinstance(configured, str):
            raw_hosts.append(configured)
        elif isinstance(configured, (list, tuple, set, frozenset)):
            raw_hosts.extend(configured)
    # Accept the original P1 top-level shape as well as the lead's `oa` entry.
    configured = registry.get("oa_pdf_domains", ())
    if isinstance(configured, str):
        raw_hosts.append(configured)
    elif isinstance(configured, (list, tuple, set, frozenset)):
        raw_hosts.extend(configured)
    hosts = {
        host for raw in raw_hosts
        if (host := _normalise_host(raw)) is not None and _public_host(host)
    }
    return hosts | set(OFFICIAL_PMC_CLOUD_DOMAINS)


def _safe_public_url(
    value: Any,
    *,
    allowed_hosts: set[str] | None = None,
    allow_fragment: bool = False,
) -> tuple[str | None, str | None]:
    """Validate an HTTPS URL without following redirects or resolving DNS."""

    if not isinstance(value, str) or not value or value != value.strip():
        return None, "fulltext_url_invalid"
    if any(ord(char) <= 0x20 or ord(char) == 0x7F for char in value):
        return None, "fulltext_url_invalid"
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except (ValueError, TypeError):
        return None, "fulltext_url_invalid"
    if parsed.scheme.lower() != "https" or not hostname:
        return None, "fulltext_url_scheme_not_allowed"
    if parsed.username is not None or parsed.password is not None:
        return None, "fulltext_url_credentials_not_allowed"
    if parsed.query or "?" in value:
        # Conservatively reject all query strings, including signed URLs and
        # opaque token parameters whose names cannot be trusted.
        return None, "signed_or_secret_url"
    if (parsed.fragment or "#" in value) and not allow_fragment:
        return None, "fulltext_url_fragment_not_allowed"
    if port not in (None, 443):
        return None, "fulltext_url_port_not_allowed"
    host = _normalise_host(hostname)
    if host is None or not _public_host(host):
        return None, "fulltext_url_private_host"
    if allowed_hosts is not None and host not in allowed_hosts:
        return None, "fulltext_host_not_allowlisted"
    return value, None


def _pdf_license(value: Any) -> tuple[str | None, bool]:
    """Return a canonical CC URL for an allowlisted URL/code and NC status."""

    if not isinstance(value, str) or not value or value != value.strip():
        return None, False
    if value in _LICENSE_URLS:
        code = value[3:]
        return _LICENSE_URLS[value], code in _NC_CODES
    try:
        parsed = urlsplit(value)
        if (parsed.scheme.lower() != "https" or parsed.hostname != "creativecommons.org"
                or parsed.username is not None or parsed.password is not None
                or parsed.port is not None or parsed.query or parsed.fragment
                or "?" in value or "#" in value):
            return None, False
    except (ValueError, TypeError):
        return None, False
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 3 or any(not part or part in {".", ".."} for part in parts):
        return None, False
    if parts[0] == "licenses" and parts[1] in _CC_CODES:
        return value, parts[1] in _NC_CODES
    if parts[0:2] == ["publicdomain", "zero"]:
        return value, False
    return None, False


def _route_pmc_source(
    doi: str,
    publisher: str | None,
    doi_prefix: str | None,
    source: Mapping[str, Any],
    *,
    noncommercial_research: bool,
) -> RoutePlan:
    raw = dict(source)
    if (raw.get("pdf_url") != raw.get("anonymous_pdf_url")
            or not isinstance(raw.get("pdf_url"), str)
            or "#" in raw["pdf_url"]):
        return _manual(doi, publisher, doi_prefix, "pmc_checked_source_mismatch", "reported_oa")
    try:
        validated = validate_pmc_metadata(
            raw,
            expected_pmid=raw.get("pmid"),
            expected_pmcid=raw.get("pmcid"),
            expected_doi=doi,
            expected_title=raw.get("title"),
            noncommercial_research=noncommercial_research,
        )
    except Exception:
        return _manual(doi, publisher, doi_prefix, "pmc_source_validation_failed", "reported_oa")

    checked_fields = (
        "pmid", "pmcid", "doi", "title", "version", "license_code",
        "anonymous_pdf_url", "expected_pdf_md5",
    )
    if any(source.get(field) != validated[field] for field in checked_fields):
        return _manual(doi, publisher, doi_prefix, "pmc_checked_source_mismatch", "reported_oa")

    license_code = _PMC_LICENSE_TO_CODE.get(str(validated["license_code"]))
    license_url = _LICENSE_URLS.get(license_code or "")
    if license_url is None:
        return _manual(doi, publisher, doi_prefix, "pmc_license_not_allowlisted", "reported_oa")
    return RoutePlan(
        doi=doi,
        publisher=publisher,
        doi_prefix=doi_prefix,
        license=license_url,
        oa_status="verified_oa",
        adapter="oa",
        action="download",
        reason="validated_pmc_oa_pdf",
        fulltext_url=str(validated["anonymous_pdf_url"]),
        pmid=str(validated["pmid"]),
        pmcid=str(validated["pmcid"]),
        title=str(validated["title"]),
        version=int(validated["version"]),
        expected_pdf_md5=str(validated["expected_pdf_md5"]),
    )


def _has_permission_evidence(entry: Mapping[str, Any]) -> bool:
    evidence = entry.get("permission_evidence")
    if isinstance(evidence, str):
        evidence_items = (evidence,)
    elif isinstance(evidence, (list, tuple, set, frozenset)):
        evidence_items = evidence
    else:
        return False
    for item in evidence_items:
        if not isinstance(item, str) or not item.strip():
            continue
        safe_reference, error = _safe_public_url(item, allow_fragment=True)
        if safe_reference is not None and error is None:
            return True
    return False


def _manual(
    doi: str | None,
    publisher: str | None,
    doi_prefix: str | None,
    reason: str,
    oa_status: str = "unknown",
) -> RoutePlan:
    return RoutePlan(
        doi=doi,
        publisher=publisher,
        doi_prefix=doi_prefix,
        license=None,
        oa_status=oa_status,
        adapter=None,
        action="manual",
        reason=reason,
        fulltext_url=None,
    )


def _call_metadata_source(
    client: MetadataClient,
    method_name: str,
    doi: str,
    preview: bool,
    *,
    optional: bool = False,
) -> tuple[Mapping[str, Any] | None, str | None]:
    method = getattr(client, method_name, None)
    if method is None and optional:
        return None, None
    if not callable(method):
        return None, f"{method_name.removeprefix('get_')}_metadata_client_invalid"
    try:
        result = method(doi, preview=preview)
    except Exception:
        # HTTP denials and other source errors are terminal; never try another
        # provider after an exception, and never retain exception text.
        return None, f"{method_name.removeprefix('get_')}_metadata_error"
    if result is None:
        return None, None
    if not isinstance(result, Mapping):
        return None, f"{method_name.removeprefix('get_')}_metadata_invalid"
    status = result.get("http_status", result.get("status_code"))
    if type(status) is int and status >= 400:
        return None, f"{method_name.removeprefix('get_')}_metadata_rejected"
    return result, None


def _source_doi_matches(metadata: Mapping[str, Any], doi: str, keys: Sequence[str]) -> bool:
    message = metadata.get("message")
    candidates = [metadata]
    if isinstance(message, Mapping):
        candidates.insert(0, message)
    for candidate in candidates:
        for key in keys:
            if key not in candidate or candidate[key] is None:
                continue
            try:
                if normalize_doi(candidate[key]) != doi:
                    return False
            except ValueError:
                return False
    return True


def _unpaywall_plan(
    doi: str,
    publisher: str | None,
    doi_prefix: str | None,
    metadata: Mapping[str, Any],
    registry: Mapping[str, Any],
    *,
    noncommercial_research: bool,
) -> RoutePlan:
    location = metadata.get("best_oa_location")
    if not isinstance(location, Mapping):
        return _manual(doi, publisher, doi_prefix, "oa_pdf_metadata_incomplete", "reported_oa")
    pdf_url = location.get("url_for_pdf")
    if not isinstance(pdf_url, str) or not pdf_url:
        return _manual(doi, publisher, doi_prefix, "oa_pdf_missing", "reported_oa")
    license_value = location.get("license_url", location.get("license"))
    license_url, is_noncommercial = _pdf_license(license_value)
    if license_url is None:
        return _manual(doi, publisher, doi_prefix, "pdf_license_not_proven", "reported_oa")
    if is_noncommercial and noncommercial_research is not True:
        return _manual(doi, publisher, doi_prefix, "noncommercial_use_not_declared", "reported_oa")
    safe_url, url_error = _safe_public_url(
        pdf_url, allowed_hosts=_allowed_oa_hosts(registry)
    )
    if safe_url is None:
        return _manual(doi, publisher, doi_prefix, url_error or "fulltext_url_invalid", "reported_oa")
    return RoutePlan(
        doi=doi,
        publisher=publisher,
        doi_prefix=doi_prefix,
        license=license_url,
        oa_status="verified_oa",
        adapter="oa",
        action="download",
        reason="verified_oa_pdf_license_and_host",
        fulltext_url=safe_url,
        title=_metadata_title(metadata),
    )


def _metadata_title(metadata: Mapping[str, Any]) -> str | None:
    value = metadata.get('title')
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if (isinstance(value, str) and value.strip() and len(value) <= 2048
            and not any(ord(c) < 32 for c in value)):
        return value.strip()
    return None


def _is_springer(publisher: str) -> bool:
    normalized = _normalise_name(publisher)
    return "springer" in normalized


def _adapter_name(publisher: str, entry: Mapping[str, Any]) -> str:
    configured = entry.get("adapter")
    if isinstance(configured, str) and re.fullmatch(r"[A-Za-z0-9_-]+", configured):
        return configured
    return publisher


def _resolve_one(
    raw_doi: str,
    registry: Mapping[str, Any],
    *,
    preview: bool,
    resolve_metadata: bool,
    metadata_client: MetadataClient | None,
    credential_present: Callable[[str], bool] | None,
    noncommercial_research: bool,
) -> RoutePlan:
    try:
        doi = normalize_doi(raw_doi)
    except ValueError:
        return _manual(None, None, None, "invalid_doi")

    entries = _registry_entries(registry)
    publisher, doi_prefix, prefix_error = _publisher_by_prefix(doi, entries)
    if not resolve_metadata:
        return _manual(doi, publisher, doi_prefix, "metadata_resolution_not_requested")
    if metadata_client is None:
        return _manual(doi, publisher, doi_prefix, "metadata_client_missing")

    pmc, source_error = _call_metadata_source(
        metadata_client, "get_pmc", doi, preview, optional=True
    )
    if source_error:
        return _manual(doi, publisher, doi_prefix, source_error)
    if pmc is not None:
        # A present PMC result is terminal: validate it locally and do not
        # switch providers if its identity, license, or object URL fails.
        return _route_pmc_source(
            doi,
            publisher,
            doi_prefix,
            pmc,
            noncommercial_research=noncommercial_research,
        )

    unpaywall, source_error = _call_metadata_source(
        metadata_client, "get_unpaywall", doi, preview
    )
    if source_error:
        return _manual(doi, publisher, doi_prefix, source_error)
    if unpaywall is not None:
        if not _source_doi_matches(unpaywall, doi, ("doi", "DOI")):
            return _manual(doi, publisher, doi_prefix, "metadata_doi_mismatch")
        if unpaywall.get("is_oa") is True:
            # An incomplete or unsafe OA claim is manual, never a reason to
            # try a second PDF URL or the publisher API.
            return _unpaywall_plan(
                doi,
                publisher,
                doi_prefix,
                unpaywall,
                registry,
                noncommercial_research=noncommercial_research,
            )

    crossref, source_error = _call_metadata_source(
        metadata_client, "get_crossref", doi, preview
    )
    if source_error:
        return _manual(doi, publisher, doi_prefix, source_error)
    if crossref is not None and not _source_doi_matches(crossref, doi, ("DOI", "doi")):
        return _manual(doi, publisher, doi_prefix, "metadata_doi_mismatch")
    if crossref is None and unpaywall is None:
        return _manual(doi, publisher, doi_prefix, "metadata_unavailable")
    if prefix_error:
        return _manual(doi, None, None, prefix_error)

    crossref_fields = _crossref_fields(crossref or {})
    if publisher is None:
        publisher, name_error = _publisher_by_name(crossref_fields.get("publisher"), entries)
        if name_error:
            return _manual(doi, None, None, name_error)
    entry = dict(entries).get(publisher) if publisher is not None else None
    if not isinstance(entry, Mapping):
        return _manual(doi, None, None, "publisher_unrecognized")

    is_oa = unpaywall.get("is_oa") if unpaywall is not None else None
    oa_status = "not_oa" if is_oa is False else "unknown"
    return _subscription_plan(
        doi,
        publisher,
        doi_prefix,
        entry,
        preview=preview,
        credential_present=credential_present,
        oa_status=oa_status,
        noncommercial_research=noncommercial_research,
        title=_metadata_title(crossref_fields) or _metadata_title(unpaywall or {}),
    )


def _subscription_plan(
    doi: str,
    publisher: str,
    doi_prefix: str | None,
    entry: Mapping[str, Any],
    *,
    preview: bool,
    credential_present: Callable[[str], bool] | None,
    oa_status: str,
    noncommercial_research: bool,
    title: str | None = None,
) -> RoutePlan:
    if entry.get("license_status") != "approved":
        return _manual(doi, publisher, doi_prefix, "publisher_not_approved", oa_status)
    if not _has_permission_evidence(entry):
        return _manual(doi, publisher, doi_prefix, "permission_evidence_missing", oa_status)
    if entry.get("endpoint_verified") is not True:
        reason = "springer_pdf_endpoint_unverified" if _is_springer(publisher) else "publisher_endpoint_unverified"
        return _manual(doi, publisher, doi_prefix, reason, oa_status)
    credential_env = entry.get("credential_env")
    if not isinstance(credential_env, str) or not _ENV_NAME_RE.fullmatch(credential_env):
        return _manual(doi, publisher, doi_prefix, "credential_presence_unavailable", oa_status)
    if preview:
        return _manual(doi, publisher, doi_prefix, "credential_presence_not_checked_in_preview", oa_status)
    if noncommercial_research is not True:
        return _manual(doi, publisher, doi_prefix, "noncommercial_use_not_declared", oa_status)
    if credential_present is None:
        return _manual(doi, publisher, doi_prefix, "credential_presence_unavailable", oa_status)
    try:
        present = credential_present(credential_env) is True
    except Exception:
        return _manual(doi, publisher, doi_prefix, "credential_presence_check_failed", oa_status)
    if not present:
        return _manual(doi, publisher, doi_prefix, "credential_missing", oa_status)
    return RoutePlan(
        doi=doi,
        publisher=publisher,
        doi_prefix=doi_prefix,
        license=None,
        oa_status=oa_status,
        adapter=_adapter_name(publisher, entry),
        action="download",
        reason="approved_permission_endpoint_and_credential",
        fulltext_url=None,
        title=title,
    )


def resolve_dois(
    dois: Sequence[str],
    registry: Mapping[str, Any],
    preview: bool = True,
    metadata_client: MetadataClient | None = None,
    credential_present: Callable[[str], bool] | None = None,
    noncommercial_research: bool = False,
    resolve_metadata: bool = False,
) -> list[RoutePlan]:
    """Build one routing plan per DOI without performing downloads.

    The default call performs no callbacks, network access, key checks, or
    writes. The injected client is not called unless the caller explicitly
    sets ``resolve_metadata=True``. Then the router calls optional
    ``get_pmc``, followed as needed by ``get_unpaywall`` and ``get_crossref``;
    each receives the DOI and ``preview`` flag at most once. A source error is
    terminal, with no retry or provider fallback. Subscription credential
    checks are never called in preview mode. This function never downloads or
    writes.
    """

    if isinstance(dois, (str, bytes)) or not isinstance(dois, Sequence):
        raise TypeError("dois must be a sequence of DOI strings")
    if not isinstance(registry, Mapping):
        raise TypeError("registry must be a mapping")
    if preview is not True and preview is not False:
        raise TypeError("preview must be bool")
    if resolve_metadata is not True and resolve_metadata is not False:
        raise TypeError("resolve_metadata must be bool")
    if noncommercial_research is not True and noncommercial_research is not False:
        raise TypeError("noncommercial_research must be bool")
    return [
        _resolve_one(
            doi,
            registry,
            preview=preview,
            resolve_metadata=resolve_metadata,
            metadata_client=metadata_client,
            credential_present=credential_present,
            noncommercial_research=noncommercial_research,
        )
        for doi in dois
    ]

