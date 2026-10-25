"""Fail-closed transport, PDF validation, and persistence for publisher APIs.

The injected transport contract is deliberately small:

    transport.get(url, *, headers, timeout, allow_redirects=False) -> response

The response exposes an integer ``status``/``status_code``/``code``, a headers
mapping, ``read(size)`` and ``close()``. The default urllib transport disables
ambient proxies and redirects, uses verified TLS, and rejects DNS answers that
are not globally routable. Injected transports own the same network safeguards;
offline test stubs do no DNS lookup.

Callers must instantiate adapters from a freshly reloaded registry entry for
each execution. This module intentionally does not load publisher_registry.json
or inspect environment variables until an approved subscription fetch reaches
its credential gate.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import tempfile
import warnings
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlsplit

from download_guard import GuardError
from download_ledger import DownloadLedger, verify_pdf
from download_jobs import DownloadJobStore
from paper_bundle import ensure_bundle, finish_download
from process_lock import DownloadInUse, process_lock
from existing_pdf_preflight import check_existing_title_pdf
from pdf_article_identity import ArticleIdentityError, compact, expected_identity, verify_article_identity


MAX_PDF_BYTES = 64 * 1024 * 1024
DAILY_ATTEMPT_LIMIT = 100
GLOBAL_PROCESS_LOCK = Path(__file__).resolve().parents[1] / "paper_downloader-global"
_CHUNK_BYTES = 64 * 1024
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_DOI_PATTERN = re.compile(r"10\.\d{4,9}/[A-Z0-9._;()/:+\-]+\Z", re.IGNORECASE)
_PMCID = re.compile(r"PMC[0-9]+\Z", re.IGNORECASE)
_SAFE_TEXT_LIMIT = 2048


@dataclass(frozen=True, slots=True)
class SourceSpec:
    doi: str
    url: str
    source: str
    channel: str
    license: str
    permission_evidence: str | None = None
    expected_md5: str | None = None
    allow_query: bool = False
    allowed_query: str | None = None


class AdapterFailure(Exception):
    """Internal stable status/code pair; never contains transport diagnostics."""

    def __init__(self, status: str, code: str) -> None:
        self.status = status
        self.code = code


class DailyRequestBudgetError(RuntimeError):
    """Safe failure raised before I/O when a durable daily slot is unavailable."""

    def __init__(self, code: str = "DAILY_REQUEST_BUDGET_REQUIRED") -> None:
        self.code = code if code in {
            "DAILY_REQUEST_BUDGET_REQUIRED", "DAILY_REQUEST_BUDGET_EXHAUSTED"
        } else "DAILY_REQUEST_BUDGET_REQUIRED"
        super().__init__(self.code)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection whose socket uses only the previously checked IPs."""

    def __init__(self, host: str, port: int, addresses: tuple[str, ...], timeout: float):
        super().__init__(host, port=port, timeout=timeout, context=ssl.create_default_context())
        self._public_addresses = addresses

    def connect(self) -> None:
        last_error: OSError | None = None
        for address in self._public_addresses:
            raw_socket = None
            try:
                raw_socket = socket.create_connection(
                    (address, self.port), self.timeout, self.source_address
                )
                self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)
                return
            except OSError as error:
                last_error = error
                if raw_socket is not None:
                    raw_socket.close()
        if last_error is not None:
            raise last_error
        raise OSError("No public address available")


class _PinnedResponse:
    def __init__(self, response: Any, connection: _PinnedHTTPSConnection):
        self._response = response
        self._connection = connection
        self.status = response.status
        self.headers = response.headers

    def read(self, size: int) -> bytes:
        return self._response.read(size)

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            self._connection.close()


class UrllibTransport:
    """Direct HTTPS transport with checked/pinned DNS, no proxy, no redirects."""

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
        allow_redirects: bool = False,
    ):
        if allow_redirects is not False:
            raise ValueError("Redirects are disabled")
        parsed = urlsplit(url)
        host = parsed.hostname
        if (parsed.scheme.lower() != "https" or not host or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)
                or parsed.fragment):
            raise ValueError("Request URL rejected")
        addresses = _resolve_public_dns(host)
        connection = _PinnedHTTPSConnection(host, 443, addresses, timeout)
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        try:
            connection.request("GET", target, headers=dict(headers))
            return _PinnedResponse(connection.getresponse(), connection)
        except Exception:
            connection.close()
            raise


def _resolve_public_dns(host: str) -> tuple[str, ...]:
    """Return only IPs verified as globally routable, rejecting mixed answers."""

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if not address.is_global:
            raise ValueError("Non-public network address")
        return (str(address),)
    rows = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    addresses = {row[4][0] for row in rows if len(row) > 4 and row[4]}
    if not addresses or any(not ipaddress.ip_address(item).is_global for item in addresses):
        raise ValueError("Non-public DNS answer")
    return tuple(sorted(addresses))


def _plan_mapping(plan: Any) -> dict[str, Any] | None:
    try:
        if is_dataclass(plan) and not isinstance(plan, type):
            value = asdict(plan)
        elif isinstance(plan, Mapping):
            value = dict(plan)
        else:
            return None
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _normalize_doi(value: Any) -> str:
    """Strict local DOI normalization without depending on the routing module."""

    if not isinstance(value, str):
        raise ValueError("invalid DOI")
    candidate = value.strip()
    if not candidate or any(ord(char) < 0x21 or ord(char) > 0x7E for char in candidate):
        raise ValueError("invalid DOI")
    if candidate.lower().startswith(("https://", "http://")):
        try:
            parsed = urlsplit(candidate)
            host = (parsed.hostname or "").lower()
            if (parsed.scheme.lower() not in {"http", "https"}
                    or host not in {"doi.org", "dx.doi.org"}
                    or parsed.username is not None or parsed.password is not None
                    or parsed.port is not None or parsed.query or parsed.fragment):
                raise ValueError("invalid DOI")
            candidate = parsed.path.lstrip("/")
        except (TypeError, ValueError):
            raise ValueError("invalid DOI") from None
    elif candidate[:4].lower() == "doi:":
        candidate = candidate[4:].strip()
    if _DOI_PATTERN.fullmatch(candidate) is None:
        raise ValueError("invalid DOI")
    return candidate.lower()


def _values(value: Any) -> tuple[Any, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(value)
    return ()


def _clean_public_reference(value: Any) -> str | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    if len(value) > 4096 or any(ord(char) <= 0x20 or ord(char) == 0x7F for char in value):
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (parsed.scheme.lower() != "https" or not host or parsed.username is not None
            or parsed.password is not None or port not in (None, 443)
            or parsed.query or not host_is_public_literal_or_name(host)):
        return None
    return value


def host_is_public_literal_or_name(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        candidate = host.lower().rstrip(".")
        if (not candidate or candidate in {"localhost", "metadata.google.internal"}
                or candidate.endswith((".localhost", ".local", ".internal", ".test", ".invalid", ".onion"))):
            return False
        try:
            candidate.encode("idna").decode("ascii")
        except UnicodeError:
            return False
        return True
    return address.is_global


def _validated_https_url(
    value: Any,
    *,
    allowed_hosts: set[str],
    allow_query: bool = False,
    allowed_query: str | None = None,
) -> str | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    if len(value) > 8192 or any(ord(char) <= 0x20 or ord(char) == 0x7F for char in value):
        return None
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (parsed.scheme.lower() != "https" or not hostname or parsed.username is not None
            or parsed.password is not None or port not in (None, 443) or parsed.fragment):
        return None
    normalized_host = hostname.encode("idna").decode("ascii").lower().rstrip(".")
    if normalized_host not in allowed_hosts or not host_is_public_literal_or_name(normalized_host):
        return None
    if parsed.query:
        if not allow_query or allowed_query is None or parsed.query != allowed_query:
            return None
    return value


def _safe_registry_prefixes(entry: Mapping[str, Any], doi: str) -> bool:
    prefixes = _values(entry.get("doi_prefixes"))
    for raw in prefixes:
        if not isinstance(raw, str):
            continue
        prefix = raw.strip().lower().rstrip("/")
        if re.fullmatch(r"10\.\d{4,9}(?:/[a-z0-9._;()/:+\-]+)?", prefix):
            if doi == prefix or doi.startswith(prefix + "/"):
                return True
    return False


def _pdf_digest_and_parse(data: bytes) -> tuple[int, str]:
    if not isinstance(data, bytes) or len(data) < 1024 or len(data) > MAX_PDF_BYTES:
        raise AdapterFailure("INVALID_PDF", "PDF_SIZE_INVALID")
    if not data.startswith(b"%PDF-") or not data.rstrip().endswith(b"%%EOF"):
        raise AdapterFailure("INVALID_PDF", "PDF_HEADER_OR_EOF_INVALID")
    try:
        from pypdf import PdfReader

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            reader = PdfReader(__import__("io").BytesIO(data), strict=True)
            if reader.is_encrypted or len(reader.pages) < 1:
                raise ValueError("PDF has no readable pages")
    except Exception:
        raise AdapterFailure("INVALID_PDF", "PDF_PARSE_FAILED") from None
    return len(data), hashlib.sha256(data).hexdigest()


def _read_response(response: Any) -> bytes:
    headers = getattr(response, "headers", {})
    content_length = None
    try:
        raw_length = headers.get("Content-Length") if hasattr(headers, "get") else None
        if raw_length is not None:
            content_length = int(raw_length)
    except (TypeError, ValueError):
        content_length = None
    if content_length is not None and content_length > MAX_PDF_BYTES:
        raise AdapterFailure("INVALID_PDF", "PDF_SIZE_LIMIT_EXCEEDED")

    chunks = bytearray()
    reader = getattr(response, "read", None)
    iterator = getattr(response, "iter_content", None)
    if callable(reader):
        while True:
            piece = reader(_CHUNK_BYTES)
            if not piece:
                break
            if not isinstance(piece, (bytes, bytearray)):
                raise AdapterFailure("FAILED", "TRANSPORT_RESPONSE_INVALID")
            chunks.extend(piece)
            if len(chunks) > MAX_PDF_BYTES:
                raise AdapterFailure("INVALID_PDF", "PDF_SIZE_LIMIT_EXCEEDED")
    elif callable(iterator):
        for piece in iterator(chunk_size=_CHUNK_BYTES):
            if not piece:
                continue
            if not isinstance(piece, (bytes, bytearray)):
                raise AdapterFailure("FAILED", "TRANSPORT_RESPONSE_INVALID")
            chunks.extend(piece)
            if len(chunks) > MAX_PDF_BYTES:
                raise AdapterFailure("INVALID_PDF", "PDF_SIZE_LIMIT_EXCEEDED")
    else:
        piece = getattr(response, "content", None)
        if not isinstance(piece, (bytes, bytearray)):
            raise AdapterFailure("FAILED", "TRANSPORT_RESPONSE_INVALID")
        chunks.extend(piece)
        if len(chunks) > MAX_PDF_BYTES:
            raise AdapterFailure("INVALID_PDF", "PDF_SIZE_LIMIT_EXCEEDED")
    return bytes(chunks)


def _write_atomic_new(path: Path, content: bytes) -> None:
    """Publish with create-if-absent hard-link semantics; never replace a file."""

    temporary: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=".publisher-adapter-", suffix=".partial", dir=path.parent
        )
        temporary = Path(raw_path)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    except Exception:
        # Any partial/material file is deliberately retained for review.
        raise AdapterFailure("NEEDS_REVIEW", "FILE_PUBLISH_FAILED") from None
    else:
        # The atomically published destination is the durable copy.
        try:
            temporary.unlink()
        except OSError:
            pass


def _write_json_new(path: Path, record: Mapping[str, Any]) -> None:
    payload = (json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    _write_atomic_new(path, payload)


def _save_daily_attempt(
    directory: Path,
    domain: str,
    limit: int,
    state_path: Path | None = None,
) -> bool:
    """Persist one UTC daily slot; caller holds the installation-wide lock."""

    state_path = state_path or directory / ".publisher-adapter-daily-attempts.json"
    today = datetime.now(timezone.utc).date().isoformat()
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise AdapterFailure("NEEDS_REVIEW", "ATTEMPT_GUARD_UNREADABLE") from None
        if (not isinstance(state, dict) or state.get("version") != 1
                or not isinstance(state.get("domains"), dict)
                or not isinstance(state.get("date"), str)):
            raise AdapterFailure("NEEDS_REVIEW", "ATTEMPT_GUARD_INVALID")
        if state["date"] != today:
            state = {"version": 1, "date": today, "domains": {}}
    else:
        state = {"version": 1, "date": today, "domains": {}}
    count = state["domains"].get(domain, 0)
    if type(count) is not int or count < 0:
        raise AdapterFailure("NEEDS_REVIEW", "ATTEMPT_GUARD_INVALID")
    if count >= limit:
        return False
    state["domains"][domain] = count + 1
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="\n", dir=directory,
            prefix=".publisher-attempts.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(state, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, state_path)
    except Exception:
        raise AdapterFailure("NEEDS_REVIEW", "ATTEMPT_GUARD_WRITE_FAILED") from None
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
    return True


def reserve_daily_request(
    directory: str | os.PathLike[str],
    url: str,
    limit: int = 100,
) -> bool:
    """Durably reserve one per-domain daily HTTP request before network I/O.

    The caller must already hold ``paper_downloader-global`` through
    ``process_lock``. This callable is suitable for
    ``PublisherMetadataClient(budget_reserver=...)`` when partially applied to
    the same output directory used by PDF adapters.
    It stores only the UTC date, hostname, and count. All adapters and public
    metadata requests share ``.publisher-adapter-daily-attempts.json`` when they
    use the same directory, so the limit is not multiplied by client or adapter.
    The original installation-wide process lock serializes the shared ledger.

    It returns True only after a durable reservation. Exhaustion raises the
    existing ``GuardError('RATE_LIMITED', domain)``; invalid input or failure to
    persist raises ``DailyRequestBudgetError`` before any network I/O.
    """

    if not isinstance(url, str) or not url or url != url.strip():
        raise DailyRequestBudgetError()
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        raise DailyRequestBudgetError() from None
    if (parsed.scheme.lower() != "https" or not host or parsed.username is not None
            or parsed.password is not None or port not in (None, 443)
            or not host_is_public_literal_or_name(host)):
        raise DailyRequestBudgetError()
    if type(limit) is not int or not 1 <= limit <= DAILY_ATTEMPT_LIMIT:
        raise DailyRequestBudgetError()
    try:
        outdir = Path(directory).expanduser().resolve()
        state_path = outdir / ".publisher-adapter-daily-attempts.json"
        outdir.mkdir(parents=True, exist_ok=True)
        reserved = _save_daily_attempt(
            outdir, host.lower().rstrip("."), limit, state_path=state_path
        )
    except AdapterFailure as failure:
        raise DailyRequestBudgetError("DAILY_REQUEST_BUDGET_REQUIRED") from None
    except Exception:
        raise DailyRequestBudgetError() from None
    if not reserved:
        raise GuardError("RATE_LIMITED", host.lower().rstrip("."))
    return True


def make_daily_budget_reserver(
    directory: str | os.PathLike[str],
    limit: int = 100,
):
    """Bind a shared output-directory budget helper for a metadata client."""

    def budget_reserver(url: str) -> bool:
        return reserve_daily_request(directory, url, limit)

    return budget_reserver


def _safe_paper_identity(doi: str, paper: Any, plan: Mapping[str, Any]) -> dict[str, Any]:
    if paper is not None and not isinstance(paper, Mapping):
        raise AdapterFailure("manual", "PAPER_SHAPE_INVALID")
    if paper is None:
        supplied = {key: plan.get(key) for key in ("pmid", "pmcid", "title") if plan.get(key) is not None}
        if supplied.get("pmid") is not None:
            supplied.update(source="pubmed", id=supplied["pmid"])
    else:
        supplied = dict(paper)
        if 'title' not in supplied and plan.get('title') is not None:
            supplied['title'] = plan['title']
        elif (isinstance(supplied.get('title'), str) and isinstance(plan.get('title'), str)
                and compact(supplied['title']) != compact(plan['title'])):
            raise AdapterFailure('NEEDS_REVIEW', 'ARTICLE_TITLE_METADATA_CONFLICT')
    paper_doi = supplied.get("doi")
    if paper_doi is not None:
        try:
            if _normalize_doi(paper_doi) != doi:
                raise AdapterFailure("NEEDS_REVIEW", "DOI_IDENTITY_MISMATCH")
        except ValueError:
            raise AdapterFailure("NEEDS_REVIEW", "DOI_IDENTITY_INVALID") from None

    identity: dict[str, Any] = {"doi": doi}
    source = supplied.get("source")
    pubmed_id = supplied.get("pubmedid", supplied.get("pmid"))
    if pubmed_id is None and source == "pubmed":
        pubmed_id = supplied.get("id")
    if pubmed_id is not None:
        text = str(pubmed_id).strip()
        if not text.isascii() or not text.isdigit() or len(text) > 16:
            raise AdapterFailure("NEEDS_REVIEW", "PUBMED_ID_INVALID")
        identity.update(source="pubmed", id=text)
    elif source == "arxiv":
        paper_id = supplied.get("id")
        if not isinstance(paper_id, str) or not paper_id or len(paper_id) > 80:
            raise AdapterFailure("NEEDS_REVIEW", "PAPER_ID_INVALID")
        identity.update(source="arxiv", id=paper_id)
    elif source == "pubmed" and supplied.get("id") is not None:
        raise AdapterFailure("NEEDS_REVIEW", "PUBMED_ID_INVALID")

    pmcid = supplied.get("pmcid")
    if isinstance(pmcid, str) and _PMCID.fullmatch(pmcid.strip()):
        identity["pmcid"] = pmcid.strip().upper()
    title = supplied.get("title")
    if isinstance(title, str) and len(title) <= _SAFE_TEXT_LIMIT and not any(ord(c) < 32 for c in title):
        identity["title"] = title
    published = supplied.get("published")
    if isinstance(published, str) and len(published) <= 64 and not any(ord(c) < 32 for c in published):
        identity["published"] = published
    # Adapter execution must never carry a potentially signed metadata URL
    # into the long-lived ledger. DOI/PMID/PMCID are sufficient identities.
    return identity


def _provenance_path(pdf_path: Path) -> Path:
    return pdf_path.with_suffix(".provenance.json")


def _provenance_matches(
    pdf_path: Path,
    *,
    doi: str,
    source: str,
    channel: str,
    license_value: str,
    permission_evidence: str | None,
    digest: str,
) -> bool:
    sidecar = _provenance_path(pdf_path)
    try:
        record = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    return (isinstance(record, dict) and record.get("version") == 1
            and record.get("doi") == doi and record.get("source") == source
            and record.get("channel") == channel and record.get("license") == license_value
            and record.get("permission_evidence") == permission_evidence
            and record.get("sha256") == digest)


class PublisherAdapter:
    """Common, fail-closed implementation for one publisher route."""

    adapter_ids: tuple[str, ...] = ()
    source_id = "publisher"
    subscription = False
    api_domain: str | None = None
    credential_header: str | None = None
    manual_only_code: str | None = None

    def __init__(
        self,
        registry_entry: Mapping[str, Any],
        transport: Any = None,
        credential_reader: Any = None,
    ) -> None:
        self.registry_entry = deepcopy(dict(registry_entry)) if isinstance(registry_entry, Mapping) else None
        self._uses_default_transport = transport is None
        self.transport = transport if transport is not None else UrllibTransport()
        self.credential_reader = credential_reader

    @staticmethod
    def _result(
        status: str,
        code: str | None,
        *,
        source: str,
        license_value: str | None,
        pdf_path: str | None = None,
        digest: str | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "error": code,
            "publiccode": code,
            "pdf_path": pdf_path,
            "sha256": digest,
            "source": source,
            "license": license_value,
        }

    @staticmethod
    def _validated_rate_limits(entry: Mapping[str, Any]) -> dict[str, int]:
        limits = entry.get("rate_limit")
        if not isinstance(limits, Mapping):
            raise AdapterFailure("manual", "RATE_POLICY_INVALID")
        interval = limits.get("interval_seconds")
        per_minute = limits.get("per_minute")
        per_day = limits.get("per_day")
        if (isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval < 5
                or type(per_minute) is not int or not 1 <= per_minute <= 10
                or type(per_day) is not int or per_day < 1):
            raise AdapterFailure("manual", "RATE_POLICY_INVALID")
        return {
            "interval_seconds": int(interval) if float(interval).is_integer() else int(interval) + 1,
            "per_minute": per_minute,
            "per_day": min(per_day, DAILY_ATTEMPT_LIMIT),
        }

    def _subscription_policy(self, plan: Mapping[str, Any], doi: str) -> tuple[str, str, dict[str, int]]:
        entry = self.registry_entry
        if not isinstance(entry, Mapping):
            raise AdapterFailure("manual", "REGISTRY_ENTRY_INVALID")
        if entry.get("license_status") != "approved":
            raise AdapterFailure("manual", "PUBLISHER_NOT_APPROVED")
        evidence_values = _values(entry.get("permission_evidence"))
        evidence = next((_clean_public_reference(item) for item in evidence_values
                         if _clean_public_reference(item) is not None), None)
        if evidence is None:
            raise AdapterFailure("manual", "PERMISSION_EVIDENCE_MISSING")
        if entry.get("endpoint_verified") is not True:
            raise AdapterFailure("manual", "ENDPOINT_UNVERIFIED")
        if self.api_domain is None or self.api_domain not in {
                str(item).lower().rstrip(".") for item in _values(entry.get("api_domains"))}:
            raise AdapterFailure("manual", "REGISTRY_ENDPOINT_MISMATCH")
        if not _safe_registry_prefixes(entry, doi):
            raise AdapterFailure("manual", "REGISTRY_DOI_MISMATCH")
        publisher_names = {"".join(c.lower() for c in str(item) if c.isalnum())
                           for item in _values(entry.get("publisher_names"))}
        aliases = {"".join(c.lower() for c in alias if c.isalnum()) for alias in self.adapter_ids}
        if not publisher_names.intersection(aliases):
            raise AdapterFailure("manual", "REGISTRY_PUBLISHER_MISMATCH")
        entry_adapter = entry.get("adapter")
        if entry_adapter is not None and entry_adapter not in self.adapter_ids:
            raise AdapterFailure("manual", "REGISTRY_ADAPTER_MISMATCH")
        if plan.get("publisher") is not None:
            plan_publisher = "".join(c.lower() for c in str(plan["publisher"]) if c.isalnum())
            if plan_publisher not in publisher_names | aliases:
                raise AdapterFailure("manual", "PLAN_PUBLISHER_MISMATCH")
        credential_env = entry.get("credential_env")
        if not isinstance(credential_env, str) or _ENV_NAME.fullmatch(credential_env) is None:
            raise AdapterFailure("manual", "CREDENTIAL_POLICY_INVALID")
        return credential_env, evidence, self._validated_rate_limits(entry)

    def _read_credential(self, variable_name: str) -> str | None:
        try:
            if self.credential_reader is None:
                import os
                value = os.environ.get(variable_name)
            elif callable(self.credential_reader):
                value = self.credential_reader(variable_name)
            else:
                return None
        except Exception:
            return None
        if (not isinstance(value, str) or not value or value != value.strip()
                or any(ord(char) < 0x21 or ord(char) > 0x7E for char in value)):
            return None
        return value

    def _prepare_source(
        self,
        plan: Mapping[str, Any],
        paper: Any,
        doi: str,
        noncommercial_research: bool,
        permission_evidence: str | None,
    ) -> SourceSpec:
        raise NotImplementedError

    def _request_headers(self, credential: str | None) -> dict[str, str]:
        raise NotImplementedError

    def _allowed_hosts(self) -> set[str]:
        if self.api_domain:
            return {self.api_domain}
        return set()

    def _response_status(self, response: Any) -> int | None:
        value = getattr(response, "status_code", None)
        if value is None:
            value = getattr(response, "status", getattr(response, "code", None))
        return value if type(value) is int else None

    def _governor_policy_ok(self, governor: Any, limits: Mapping[str, int]) -> bool:
        if not all(callable(getattr(governor, name, None)) for name in
                   ("acquire", "record_http_error", "record_success")):
            return False
        interval = getattr(governor, "interval", None)
        per_minute = getattr(governor, "per_minute", None)
        try:
            if interval is not None and float(interval) < limits["interval_seconds"]:
                return False
            if per_minute is not None and int(per_minute) > limits["per_minute"]:
                return False
        except (TypeError, ValueError):
            return False
        return True

    def _register_job(self, identity: Mapping[str, Any], ledger: DownloadLedger) -> bool:
        if identity.get("source") != "pubmed":
            return True
        try:
            jobs = DownloadJobStore(Path(ledger.directory))
            key = jobs.ensure(dict(identity), "publisher adapter")
            job_paper = dict(identity)
            job_paper.update(status="exists", pdf_path="", sha256="")
            if jobs.prepare(key, job_paper, ledger):
                jobs.mark_fetching(key)
                jobs.record_result(key, job_paper, ledger)
            return jobs.state["jobs"][key].get("status") == "SAVED"
        except Exception:
            return False

    def _verify_existing(
        self,
        pdf_path: Path,
        *,
        spec: SourceSpec,
        ledger: DownloadLedger,
        identity: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        if not pdf_path.exists():
            return None
        try:
            if pdf_path.stat().st_size > MAX_PDF_BYTES:
                raise AdapterFailure("NEEDS_REVIEW", "EXISTING_PDF_SIZE_INVALID")
            size, digest = verify_pdf(pdf_path)
            data = pdf_path.read_bytes()
            parsed_size, parsed_digest = _pdf_digest_and_parse(data)
            if size != parsed_size or digest != parsed_digest:
                raise AdapterFailure("NEEDS_REVIEW", "EXISTING_PDF_HASH_MISMATCH")
            if not _provenance_matches(
                pdf_path, doi=spec.doi, source=spec.source, channel=spec.channel,
                license_value=spec.license, permission_evidence=spec.permission_evidence,
                digest=digest,
            ):
                raise AdapterFailure("NEEDS_REVIEW", "EXISTING_PROVENANCE_MISMATCH")
            self._validate_article(data, identity, existing=True)
            known = ledger.lookup(dict(identity))
            if known is None:
                ledger.record(dict(identity), pdf_path)
            elif Path(known["pdf_path"]).resolve() != pdf_path.resolve():
                raise AdapterFailure("NEEDS_REVIEW", "LEDGER_PDF_MISMATCH")
            if not self._register_job(identity, ledger):
                raise AdapterFailure("NEEDS_REVIEW", "DOWNLOAD_JOB_NEEDS_REVIEW")
            return self._result(
                "exists", None, source=spec.source, license_value=spec.license,
                pdf_path=str(pdf_path.resolve()), digest=digest,
            )
        except AdapterFailure:
            raise
        except Exception:
            raise AdapterFailure("NEEDS_REVIEW", "EXISTING_FILE_NEEDS_REVIEW") from None

    def _existing_ledger_result(
        self,
        known: Mapping[str, Any],
        *,
        spec: SourceSpec,
        ledger: DownloadLedger,
        identity: Mapping[str, Any],
    ) -> dict[str, Any]:
        pdf_path = Path(str(known.get("pdf_path", "")))
        digest = known.get("sha256")
        try:
            if pdf_path.stat().st_size > MAX_PDF_BYTES:
                raise AdapterFailure("NEEDS_REVIEW", "EXISTING_PDF_SIZE_INVALID")
            size, actual_digest = verify_pdf(pdf_path)
            data = pdf_path.read_bytes()
            parsed_size, parsed_digest = _pdf_digest_and_parse(data)
            if size != parsed_size or actual_digest != parsed_digest or actual_digest != digest:
                raise AdapterFailure("NEEDS_REVIEW", "EXISTING_PDF_HASH_MISMATCH")
        except AdapterFailure:
            raise
        except Exception:
            raise AdapterFailure("NEEDS_REVIEW", "EXISTING_FILE_NEEDS_REVIEW") from None
        if (not isinstance(digest, str) or not _provenance_matches(
                pdf_path, doi=spec.doi, source=spec.source, channel=spec.channel,
                license_value=spec.license, permission_evidence=spec.permission_evidence,
                digest=digest)):
            raise AdapterFailure("NEEDS_REVIEW", "EXISTING_PROVENANCE_MISMATCH")
        self._validate_article(data, identity, existing=True)
        if not self._register_job(identity, ledger):
            raise AdapterFailure("NEEDS_REVIEW", "DOWNLOAD_JOB_NEEDS_REVIEW")
        return self._result("exists", None, source=spec.source, license_value=spec.license,
                            pdf_path=str(pdf_path), digest=digest)

    def _validate_article(self, data, identity, *, existing=False):
        try:
            return verify_article_identity(data, doi=identity.get('doi'), title=identity.get('title'))
        except ArticleIdentityError as failure:
            status = 'NEEDS_REVIEW' if existing or failure.code in {
                'ARTICLE_TITLE_REQUIRED', 'ARTICLE_DOI_INVALID', 'ARTICLE_TEXT_UNAVAILABLE'
            } else 'INVALID_PDF'
            raise AdapterFailure(status, failure.code) from None

    def _http_error(self, governor: Any, url: str, status: int, headers: Any) -> str:
        if status in {401, 403, 406, 429} or 500 <= status <= 599:
            retry_after = None
            try:
                retry_after = headers.get("Retry-After") if hasattr(headers, "get") else None
            except Exception:
                retry_after = None
            try:
                # Preserve the existing governor unchanged: uncommon 5xx
                # responses use its generic 500 cooldown/error-count policy.
                guard_status = status if status in {401, 403, 406, 429, 500, 502, 503, 504} else 500
                guard_error = governor.record_http_error(
                    url, guard_status, retry_after, authenticated=(status == 401)
                )
            except Exception:
                return "NEEDS_REVIEW"
            if status == 401:
                return "AUTH_REQUIRED"
            if status == 403:
                return "ACCESS_DENIED"
            if guard_error is None:
                return "RATE_LIMITED"
            safe_status = getattr(guard_error, "status", None)
            return safe_status if safe_status in {"DOMAIN_PAUSED", "RETRY_WAIT", "RATE_LIMITED"} else "RATE_LIMITED"
        return "manual"

    def _follow_redirect(self, response, *, spec, directory, governor, limits):
        """Default remains closed; only an explicit adapter may allow one hop.

        On success the implementation closes the original response and returns
        the new response and its actual URL. The caller closes the final one.
        A failing implementation must close any second response it acquired.
        No credentials are passed to this hook.
        """
        raise AdapterFailure("manual", "REDIRECT_BLOCKED")

    def _validate_pdf_response(self, response):
        """Optional publisher MIME gate, before reading or publishing a PDF."""
        return None

    def fetch_pdf(
        self, plan, outdir, governor, *, paper=None,
        noncommercial_research=False, expected_title=None,
    ):
        from acquisition_gateway import dispatch_selected
        channel = "public_fulltext" if self.source_id.startswith("fulltext:") else "publisher" if self.subscription else "oa"
        return dispatch_selected(channel, "main_pdf", lambda:
            self._fetch_pdf_selected(plan, outdir, governor, paper=paper,
                noncommercial_research=noncommercial_research, expected_title=expected_title))

    def _fetch_pdf_selected(
        self,
        plan: Any,
        outdir: str | os.PathLike[str],
        governor: Any,
        *,
        paper: Any = None,
        noncommercial_research: bool = False,
        expected_title: str | None = None,
    ) -> dict[str, Any]:
        plan_data = _plan_mapping(plan)
        if plan_data is None:
            return self._result("manual", "PLAN_SHAPE_INVALID", source=self.source_id, license_value=None)
        route_adapter = plan_data.get("adapter")
        if plan_data.get("action") != "download":
            return self._result("manual", "ROUTE_NOT_DOWNLOADABLE", source=self.source_id, license_value=None)
        if route_adapter not in self.adapter_ids:
            return self._result("manual", "ADAPTER_IDENTITY_MISMATCH", source=self.source_id, license_value=None)
        if self.manual_only_code is not None:
            return self._result("manual", self.manual_only_code, source=self.source_id, license_value=None)
        try:
            doi = _normalize_doi(plan_data.get("doi"))
        except (TypeError, ValueError):
            return self._result("manual", "DOI_INVALID", source=self.source_id, license_value=None)

        permission_evidence = None
        limits = {"interval_seconds": 5, "per_minute": 10, "per_day": DAILY_ATTEMPT_LIMIT}
        credential = None
        if self.subscription:
            try:
                credential_env, permission_evidence, limits = self._subscription_policy(plan_data, doi)
            except AdapterFailure as failure:
                return self._result(failure.status, failure.code, source=self.source_id, license_value=None)
            if noncommercial_research is not True:
                return self._result('manual', 'NONCOMMERCIAL_RESEARCH_REQUIRED',
                                    source=self.source_id, license_value=None)
        else:
            try:
                if not isinstance(self.registry_entry, Mapping):
                    raise AdapterFailure("manual", "REGISTRY_ENTRY_INVALID")
                limits = self._validated_rate_limits(self.registry_entry)
            except AdapterFailure as failure:
                return self._result(failure.status, failure.code, source=self.source_id, license_value=None)
        try:
            spec = self._prepare_source(
                plan_data, paper, doi, noncommercial_research, permission_evidence
            )
        except AdapterFailure as failure:
            return self._result(failure.status, failure.code, source=self.source_id, license_value=None)
        except Exception:
            return self._result("manual", "ROUTE_METADATA_INVALID", source=self.source_id, license_value=None)

        try:
            allowed_hosts = self._allowed_hosts()
            if _validated_https_url(
                    spec.url, allowed_hosts=allowed_hosts, allow_query=spec.allow_query,
                    allowed_query=spec.allowed_query) is None:
                raise AdapterFailure("manual", "REQUEST_URL_REJECTED")
        except AdapterFailure as failure:
            return self._result(failure.status, failure.code, source=spec.source, license_value=spec.license)
        except Exception:
            return self._result("manual", "REQUEST_URL_REJECTED", source=spec.source, license_value=spec.license)

        try:
            identity = _safe_paper_identity(doi, paper, plan_data)
            for local_title in (identity.get('title'), expected_title):
                local_check = check_existing_title_pdf(outdir, doi=doi, title=local_title)
                if local_check['status'] == 'review_required':
                    return self._result('NEEDS_REVIEW', local_check['reason_code'],
                                        source=spec.source, license_value=spec.license)
            try:
                expected_identity(identity.get('doi'), identity.get('title'))
            except ArticleIdentityError as failure:
                raise AdapterFailure('NEEDS_REVIEW', failure.code) from None
        except AdapterFailure as failure:
            return self._result(failure.status, failure.code, source=spec.source, license_value=spec.license)
        except Exception:
            return self._result('NEEDS_REVIEW', 'EXISTING_PDF_PREFLIGHT_UNAVAILABLE',
                                source=spec.source, license_value=spec.license)

        if self.subscription:
            credential = self._read_credential(credential_env)
            if credential is None:
                return self._result("AUTH_REQUIRED", "CREDENTIAL_MISSING", source=spec.source, license_value=spec.license)
        try:
            headers = self._request_headers(credential)
        except AdapterFailure as failure:
            return self._result(failure.status, failure.code, source=spec.source, license_value=spec.license)
        except Exception:
            return self._result("NEEDS_REVIEW", "REQUEST_HEADERS_INVALID", source=spec.source, license_value=spec.license)
        if not isinstance(headers, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()):
            return self._result("NEEDS_REVIEW", "REQUEST_HEADERS_INVALID", source=spec.source, license_value=spec.license)
        if not self._governor_policy_ok(governor, limits):
            return self._result("manual", "RATE_POLICY_MISMATCH", source=spec.source, license_value=spec.license)
        try:
            directory = Path(outdir).resolve()
            directory.mkdir(parents=True, exist_ok=True)
        except Exception:
            return self._result("NEEDS_REVIEW", "OUTPUT_DIRECTORY_UNAVAILABLE", source=spec.source, license_value=spec.license)
        try:
            with process_lock(GLOBAL_PROCESS_LOCK):
                try:
                    ledger = DownloadLedger(directory)
                    known = ledger.lookup(identity)
                    if known is not None:
                        return self._existing_ledger_result(
                            known, spec=spec, ledger=ledger, identity=identity
                        )
                except AdapterFailure as failure:
                    return self._result(failure.status, failure.code, source=spec.source, license_value=spec.license)
                except Exception:
                    return self._result("NEEDS_REVIEW", "LEDGER_NEEDS_REVIEW", source=spec.source, license_value=spec.license)

                filename = "doi-" + hashlib.sha256(doi.encode("ascii")).hexdigest() + ".pdf"
                legacy_path = directory / filename
                pdf_path = (legacy_path if legacy_path.exists() else
                            ensure_bundle(directory, identity, create=False) / filename)
                if pdf_path.exists():
                    try:
                        existing = self._verify_existing(
                            pdf_path, spec=spec, ledger=ledger, identity=identity
                        )
                    except AdapterFailure as failure:
                        return self._result(failure.status, failure.code, source=spec.source, license_value=spec.license)
                    if existing is not None:
                        return existing

                # Recheck after locking and existing-ledger/canonical verification;
                # a candidate appearing since the pre-credential check must stop
                # before a daily reservation or physical request. Not an atomic
                # guarantee against edits by non-cooperating external programs.
                for local_title in (identity.get('title'), expected_title):
                    local_check = check_existing_title_pdf(directory, doi=doi, title=local_title)
                    if local_check['status'] == 'review_required':
                        return self._result('NEEDS_REVIEW', local_check['reason_code'],
                                            source=spec.source, license_value=spec.license)
                try:
                    reserve_daily_request(directory, spec.url, limits["per_day"])
                except GuardError as failure:
                    safe_status = getattr(failure, "status", None)
                    safe_status = safe_status if safe_status == "RATE_LIMITED" else "RATE_LIMITED"
                    return self._result(safe_status, safe_status, source=spec.source, license_value=spec.license)
                except DailyRequestBudgetError as failure:
                    return self._result("NEEDS_REVIEW", failure.code, source=spec.source, license_value=spec.license)

                try:
                    governor.acquire(spec.url, download_attempt=True)
                except GuardError as failure:
                    safe_status = getattr(failure, "status", None)
                    if safe_status not in {"AUTH_EXPIRED", "ACCESS_DENIED", "DOMAIN_PAUSED", "RATE_LIMITED", "RETRY_WAIT"}:
                        safe_status = "RATE_LIMITED"
                    public_status = "AUTH_REQUIRED" if safe_status == "AUTH_EXPIRED" else safe_status
                    return self._result(public_status, public_status, source=spec.source, license_value=spec.license)
                except Exception:
                    return self._result("NEEDS_REVIEW", "GOVERNOR_ACQUIRE_FAILED", source=spec.source, license_value=spec.license)

                response = None
                try:
                    response = self.transport.get(
                        spec.url, headers=headers, timeout=30, allow_redirects=False
                    )
                except Exception:
                    return self._result("FAILED", "TRANSPORT_ERROR", source=spec.source, license_value=spec.license)
                try:
                    status = self._response_status(response)
                    response_url = spec.url
                    if status is None:
                        return self._result("FAILED", "TRANSPORT_RESPONSE_INVALID", source=spec.source, license_value=spec.license)
                    if status in {301, 302, 303, 307, 308}:
                        try:
                            response, response_url = self._follow_redirect(response,
                                spec=spec, directory=directory, governor=governor, limits=limits)
                            status = self._response_status(response)
                        except AdapterFailure as failure:
                            return self._result(failure.status, failure.code,
                                source=spec.source, license_value=spec.license)
                        except Exception:
                            return self._result("NEEDS_REVIEW", "REDIRECT_HANDOFF_FAILED",
                                source=spec.source, license_value=spec.license)
                        if status is None:
                            return self._result("FAILED", "TRANSPORT_RESPONSE_INVALID",
                                source=spec.source, license_value=spec.license)
                        if status in {301, 302, 303, 307, 308}:
                            return self._result("manual", "REDIRECT_CHAIN_BLOCKED",
                                source=spec.source, license_value=spec.license)
                    if status != 200:
                        safe_status = self._http_error(
                            governor, response_url, status, getattr(response, "headers", {})
                        )
                        code = "HTTP_" + str(status) if safe_status == "manual" else safe_status
                        return self._result(safe_status, code, source=spec.source, license_value=spec.license)
                    try:
                        # Never pass a signed redirect URL to control/log hooks.
                        parsed_response_url = urlsplit(response_url)
                        success_url = (spec.url if response_url == spec.url else
                                       "https://" + parsed_response_url.hostname + "/")
                        governor.record_success(success_url)
                    except Exception:
                        return self._result("NEEDS_REVIEW", "GOVERNOR_STATE_FAILED", source=spec.source, license_value=spec.license)
                    try:
                        self._validate_pdf_response(response)
                        pdf_bytes = _read_response(response)
                        size, digest = _pdf_digest_and_parse(pdf_bytes)
                        self._validate_article(pdf_bytes, identity)
                    except AdapterFailure as failure:
                        return self._result(failure.status, failure.code, source=spec.source, license_value=spec.license)
                    except Exception:
                        return self._result("FAILED", "TRANSPORT_BODY_ERROR", source=spec.source, license_value=spec.license)
                finally:
                    close = getattr(response, "close", None)
                    if callable(close):
                        try:
                            close()
                        except Exception:
                            pass

                if spec.expected_md5 is not None and hashlib.md5(pdf_bytes).hexdigest() != spec.expected_md5:
                    return self._result("INVALID_PDF", "PMC_CHECKSUM_MISMATCH", source=spec.source, license_value=spec.license)
                try:
                    pdf_path.parent.mkdir(parents=True, exist_ok=True)
                    _write_atomic_new(pdf_path, pdf_bytes)
                except AdapterFailure as failure:
                    if pdf_path.exists():
                        try:
                            existing = self._verify_existing(
                                pdf_path, spec=spec, ledger=ledger, identity=identity
                            )
                            if existing is not None:
                                return existing
                        except AdapterFailure:
                            pass
                    return self._result(failure.status, failure.code, source=spec.source, license_value=spec.license)

                record = {
                    "version": 1,
                    "doi": doi,
                    "source": spec.source,
                    "channel": spec.channel,
                    "license": spec.license,
                    "permission_evidence": spec.permission_evidence,
                    "sha256": digest,
                    "size_bytes": size,
                }
                try:
                    _write_json_new(_provenance_path(pdf_path), record)
                except AdapterFailure as failure:
                    return self._result(failure.status, "PROVENANCE_WRITE_FAILED", source=spec.source, license_value=spec.license, pdf_path=str(pdf_path), digest=digest)
                try:
                    ledger.record(identity, pdf_path)
                except Exception:
                    return self._result("NEEDS_REVIEW", "LEDGER_WRITE_FAILED", source=spec.source, license_value=spec.license, pdf_path=str(pdf_path), digest=digest)
                if not self._register_job(identity, ledger):
                    return self._result("NEEDS_REVIEW", "DOWNLOAD_JOB_NEEDS_REVIEW", source=spec.source, license_value=spec.license, pdf_path=str(pdf_path), digest=digest)
                result = self._result(
                    "downloaded", None, source=spec.source, license_value=spec.license,
                    pdf_path=str(pdf_path.resolve()), digest=digest,
                )
                acquisition = dict(paper) if isinstance(paper, Mapping) else dict(identity)
                acquisition.update(identity)
                result.update(finish_download(directory, acquisition, pdf_path,
                              governor=governor, source=spec.source, license_value=spec.license))
                return result
        except DownloadInUse:
            return self._result("manual", "DOWNLOAD_ALREADY_IN_PROGRESS", source=spec.source, license_value=spec.license)
        except Exception:
            return self._result("NEEDS_REVIEW", "ADAPTER_STATE_NEEDS_REVIEW", source=spec.source, license_value=spec.license)
