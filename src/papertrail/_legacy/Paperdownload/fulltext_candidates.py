"""Discover full-body links from supplied public metadata; never authorize access.

No network, credentials, environment reads, or state changes. ValueError messages
are stable codes. Unsafe URLs are discarded; malformed metadata fails closed.
"""

import unicodedata
from urllib.parse import urlsplit

from fulltext_url import europepmc_pdf_api_url, validated_public_fulltext_url
from publisher_router import normalize_doi as _router_normalize_doi


MAX_CANDIDATES = 32
MAX_URL = 8192
MAX_TITLE = 2048
_API_HOSTS = {"api.elsevier.com", "api.wiley.com", "api.springernature.com"}
_MISSING = object()


def _text(value, limit=MAX_URL, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise ValueError("INVALID_RESPONSE")
    if len(value) > limit:
        raise ValueError("INVALID_LENGTH")
    if any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in value):
        raise ValueError("INVALID_TEXT")
    return value


def normalize_doi(value):
    """Use the CLI/download identity contract with this module's stable code."""
    try:
        return _router_normalize_doi(value)
    except ValueError:
        raise ValueError("INVALID_DOI") from None


def _validate_tree(value, depth=0, budget=None):
    # Bound work on arbitrary caller-supplied objects, including cyclic containers.
    if budget is None:
        budget = [20000, 8 * 1024 * 1024]
    budget[0] -= 1
    if budget[0] < 0 or depth > 24:
        raise ValueError("INVALID_RESPONSE")
    if isinstance(value, str):
        # Coarse upper bound for UTF-8 bytes; ignored abstracts may contain
        # legitimate newlines and exceed candidate-field length limits.
        budget[1] -= len(value) * 4
        if budget[1] < 0:
            raise ValueError("INVALID_RESPONSE")
    elif isinstance(value, dict):
        if len(value) > 4096:
            raise ValueError("INVALID_RESPONSE")
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("INVALID_RESPONSE")
            _validate_tree(key, depth + 1, budget)
            _validate_tree(item, depth + 1, budget)
    elif isinstance(value, list):
        if len(value) > 4096:
            raise ValueError("INVALID_RESPONSE")
        for item in value:
            _validate_tree(item, depth + 1, budget)
    elif value is not None and type(value) not in (bool, int, float):
        raise ValueError("INVALID_RESPONSE")


def _object(value):
    if not isinstance(value, dict):
        raise ValueError("INVALID_RESPONSE")
    return value


def _list(value):
    if not isinstance(value, list):
        raise ValueError("INVALID_RESPONSE")
    return value


def _match(record, field, expected):
    if field not in record:
        raise ValueError("INVALID_RESPONSE")
    if normalize_doi(record[field]) != expected:
        raise ValueError("DOI_MISMATCH")


def _optional(record, field, limit=MAX_TITLE):
    return _text(record.get(field), limit, nullable=True)


def _candidate(provider, doi, title, url, fmt, availability, license_value,
               version=None, intended=None):
    safe = validated_public_fulltext_url(_text(url)) if url is not None else None
    if safe is None:
        return None
    return {
        "provider": provider, "doi": doi, "title": title, "url": safe,
        "format": fmt, "availability": availability, "license": license_value,
        "version": version, "intended_application": intended,
        "access_route": "publisher_api" if urlsplit(safe).hostname in _API_HOSTS else "direct",
    }


def _openalex(response, doi):
    _match(response, "doi", doi)
    title = _text(response.get("title"), MAX_TITLE)
    for location in _list(response.get("locations")):
        location = _object(location)
        oa = location.get("is_oa")
        if oa is not None and type(oa) is not bool:
            raise ValueError("INVALID_RESPONSE")
        license_value = _optional(location, "license", MAX_URL)
        version = _optional(location, "version")
        yield _candidate("openalex", doi, title, location.get("pdf_url"), "pdf",
                         "open_access" if oa is True else "unknown", license_value, version)


def _europepmc(response, doi):
    count = response.get("hitCount")
    if type(count) is not int or count < 0:
        raise ValueError("INVALID_RESPONSE")
    records = _list(_object(response.get("resultList")).get("result"))
    if count < len(records) or (count > 0 and not records):
        raise ValueError("INVALID_RESPONSE")
    for record in records:
        record = _object(record)
        _match(record, "doi", doi)
        title = _text(record.get("title"), MAX_TITLE)
        oa = record.get("isOpenAccess")
        if oa not in (None, "Y", "N"):
            raise ValueError("INVALID_RESPONSE")
        api_pdf = europepmc_pdf_api_url(record)
        if api_pdf:
            yield _candidate("europepmc", doi, title, api_pdf, "pdf", "open_access",
                             _optional(record, "license", MAX_URL), _optional(record, "version"))
        links = record.get("fullTextUrlList", _MISSING)
        if links is _MISSING:
            continue
        links = _list(_object(links).get("fullTextUrl"))
        for link in links:
            link = _object(link)
            style = _optional(link, "documentStyle")
            if style not in ("pdf", "xml"):
                continue
            url = link.get("url")
            if url is not None:
                _text(url)
            label = _optional(link, "availability")
            license_value = _text(link.get("license", record.get("license")), MAX_URL, nullable=True)
            version = _text(link.get("version", record.get("version")), MAX_TITLE, nullable=True)
            if "url" not in link or url is None:
                raise ValueError("INVALID_RESPONSE")
            availability = {
                "open access": "open_access", "free": "free", "free to read": "free",
                "subscription required": "subscription",
            }.get(label.lower() if label else "", "open_access" if oa == "Y" else "unknown")
            yield _candidate("europepmc", doi, title, url, style, availability, license_value, version)


def _crossref(response, doi):
    if response.get("status", "ok") != "ok":
        raise ValueError("PROVIDER_ERROR")
    if response.get("message-type", "work") != "work":
        raise ValueError("INVALID_RESPONSE")
    record = _object(response.get("message"))
    _match(record, "DOI", doi)
    titles = _list(record.get("title"))
    for title in titles:
        _text(title, MAX_TITLE)
    title = titles[0] if titles else ""
    licenses = []
    for entry in _list(record.get("license", [])):
        entry = _object(entry)
        licenses.append((_optional(entry, "content-version"), _optional(entry, "URL", MAX_URL)))
    for link in _list(record.get("link", [])):
        link = _object(link)
        url = _text(link.get("URL"))
        content_type = _text(link.get("content-type"))
        version = _optional(link, "content-version")
        intended = _optional(link, "intended-application")
        explicit_license = _optional(link, "license", MAX_URL)
        matching = {value for lic_version, value in licenses
                    if version is not None and lic_version == version and value is not None}
        license_value = explicit_license if explicit_license is not None else (
            next(iter(matching)) if len(matching) == 1 else None)
        fmt = {"application/pdf": "pdf", "text/xml": "xml", "application/xml": "xml"}.get(content_type)
        if (content_type == "unspecified" and validated_public_fulltext_url(url) is not None
                and "/articlepdf/" in urlsplit(url).path):
            fmt = "unknown"
        if fmt is not None:
            yield _candidate("crossref", doi, title, url, fmt, "unknown", license_value, version, intended)


def parse_candidates(provider, response, doi):
    """Return <=32 distinct candidates. Discovery is not download authorization.

    Stable errors: INVALID_DOI, DOI_MISMATCH, INVALID_PROVIDER,
    INVALID_RESPONSE, PROVIDER_ERROR, INVALID_TEXT, INVALID_LENGTH,
    TOO_MANY_CANDIDATES. No unsafe URL is converted or repaired.
    """
    doi = normalize_doi(doi)
    parsers = {"openalex": _openalex, "europepmc": _europepmc, "crossref": _crossref}
    if not isinstance(provider, str) or provider not in parsers:
        raise ValueError("INVALID_PROVIDER")
    response = _object(response)
    _validate_tree(response)
    if "errCode" in response or "error" in response:
        raise ValueError("PROVIDER_ERROR")
    candidates, seen = [], set()
    for candidate in parsers[provider](response, doi):
        if candidate is None:
            continue
        key = (provider, candidate["url"], candidate["format"])
        if key not in seen:
            if len(candidates) == MAX_CANDIDATES:
                raise ValueError("TOO_MANY_CANDIDATES")
            seen.add(key)
            candidates.append(candidate)
    return candidates
