"""Springer Nature JATS retrieval, kept separate from the PDF adapter path."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET
from typing import Any, Mapping
from urllib.parse import quote, quote_plus, urlencode, urlsplit

from download_guard import GuardError, RequestGovernor
from process_lock import DownloadInUse, process_lock

from .base import (
    AdapterFailure,
    GLOBAL_PROCESS_LOCK,
    PublisherAdapter,
    _clean_public_reference,
    _normalize_doi,
    _safe_registry_prefixes,
    _values,
    _validated_https_url,
    _write_atomic_new,
    _write_json_new,
    reserve_daily_request,
)


API_DOMAIN = "api.springernature.com"
FULLTEXT_ENDPOINT = f"https://{API_DOMAIN}/xmldata/jats"
SAFE_API_ORIGIN = f"https://{API_DOMAIN}/"
FULLTEXT_DOCUMENTATION = "https://dev.springernature.com/docs/api-endpoints/fulltext-api/"
MIGRATION_DOCUMENTATION = "https://dev.springernature.com/docs/full-text-api-migration-guide/"
MAX_JATS_BYTES = 16 * 1024 * 1024
_CHUNK_BYTES = 64 * 1024
_DTD_OR_ENTITY = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_XML_ENCODING = re.compile(r"^\s*<\?xml\s+[^>]*?encoding\s*=\s*(['\"])([^'\"]+)\1", re.IGNORECASE)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _read_jats_response(response: Any) -> bytes:
    """Read a bounded response body without trusting Content-Length."""

    headers = getattr(response, "headers", {})
    try:
        raw_length = headers.get("Content-Length") if hasattr(headers, "get") else None
        if raw_length is not None and int(raw_length) > MAX_JATS_BYTES:
            raise AdapterFailure("INVALID_JATS", "JATS_SIZE_LIMIT_EXCEEDED")
    except AdapterFailure:
        raise
    except (TypeError, ValueError):
        pass

    reader = getattr(response, "read", None)
    if not callable(reader):
        raise AdapterFailure("FAILED", "TRANSPORT_RESPONSE_INVALID")
    chunks = bytearray()
    while True:
        piece = reader(min(_CHUNK_BYTES, MAX_JATS_BYTES + 1 - len(chunks)))
        if not piece:
            break
        if not isinstance(piece, (bytes, bytearray)):
            raise AdapterFailure("FAILED", "TRANSPORT_RESPONSE_INVALID")
        chunks.extend(piece)
        if len(chunks) > MAX_JATS_BYTES:
            raise AdapterFailure("INVALID_JATS", "JATS_SIZE_LIMIT_EXCEEDED")
    if not chunks:
        raise AdapterFailure("INVALID_JATS", "JATS_EMPTY_RESPONSE")
    return bytes(chunks)


def _response_content_type(response: Any) -> str | None:
    headers = getattr(response, "headers", {})
    if not hasattr(headers, "items"):
        return None
    try:
        for key, value in headers.items():
            if isinstance(key, str) and key.lower() == "content-type":
                return value.split(";", 1)[0].strip().lower() if isinstance(value, str) else ""
    except Exception:
        return ""
    return None


def _credential_echo_tokens(credential: str) -> tuple[bytes, ...]:
    """Known one-pass URL encodings of a KEY/METRIC value and its components."""

    tokens = {credential, quote(credential, safe=""), quote_plus(credential, safe="")}
    for part in credential.split("/"):
        if len(part) >= 8:
            tokens.add(part)
        for encoded in (quote(part, safe=""), quote_plus(part, safe="")):
            if encoded != part:
                tokens.add(encoded)
    return tuple(token.encode("ascii") for token in tokens if token)


def _validate_jats_article(payload: bytes, requested_doi: str) -> None:
    """Validate one full-text article using the supported UTF-8, no-DTD XML subset.

    UTF-16/32 payloads and all DTD/ENTITY declarations are intentionally refused,
    including otherwise valid JATS documents that depend on an external DTD.
    """

    if not isinstance(payload, bytes) or not payload or len(payload) > MAX_JATS_BYTES:
        raise AdapterFailure("INVALID_JATS", "JATS_SIZE_OR_EMPTY")
    try:
        decoded = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise AdapterFailure("INVALID_JATS", "JATS_XML_ENCODING_UNSUPPORTED") from None
    if "\x00" in decoded:
        raise AdapterFailure("INVALID_JATS", "JATS_XML_ENCODING_UNSUPPORTED")
    declaration = _XML_ENCODING.search(decoded)
    if declaration is not None and declaration.group(2).lower().replace("_", "-") not in {"utf-8", "utf8"}:
        raise AdapterFailure("INVALID_JATS", "JATS_XML_ENCODING_UNSUPPORTED")
    if _DTD_OR_ENTITY.search(decoded):
        raise AdapterFailure("INVALID_JATS", "JATS_XML_SECURITY_REJECTED")
    try:
        root = ET.fromstring(payload)
    except (ET.ParseError, ValueError, LookupError):
        raise AdapterFailure("INVALID_JATS", "JATS_XML_INVALID") from None

    articles = [node for node in root.iter() if _local_name(node.tag) == "article"]
    if len(articles) != 1:
        raise AdapterFailure("INVALID_JATS", "JATS_ARTICLE_COUNT_INVALID")
    article = articles[0]

    article_meta = [node for node in article.iter() if _local_name(node.tag) == "article-meta"]
    doi_ids = []
    for metadata in article_meta:
        for node in metadata.iter():
            if (_local_name(node.tag) == "article-id"
                    and str(node.attrib.get("pub-id-type", "")).lower() == "doi"
                    and isinstance(node.text, str) and node.text.strip()):
                doi_ids.append(node.text.strip())
    if len(doi_ids) != 1:
        raise AdapterFailure("INVALID_JATS", "JATS_ARTICLE_DOI_INVALID")
    try:
        response_doi = _normalize_doi(doi_ids[0])
    except (TypeError, ValueError):
        raise AdapterFailure("INVALID_JATS", "JATS_ARTICLE_DOI_INVALID") from None
    if response_doi != requested_doi:
        raise AdapterFailure("INVALID_JATS", "JATS_DOI_MISMATCH")

    bodies = [node for node in article.iter() if _local_name(node.tag) == "body"]
    if len(bodies) != 1:
        raise AdapterFailure("INVALID_JATS", "JATS_FULLTEXT_BODY_MISSING")
    body = bodies[0]
    paragraphs = [node for node in body.iter() if _local_name(node.tag) == "p"]
    paragraph_text = "".join("".join(node.itertext()) for node in paragraphs)
    if len(re.sub(r"\s+", "", paragraph_text)) < 40:
        raise AdapterFailure("INVALID_JATS", "JATS_FULLTEXT_BODY_MISSING")


def openaccess_jats_validation_view(payload: bytes, requested_doi: str) -> bytes:
    """Validate OA API XML without resolving its two fixed DTD declarations.

    The OA response envelope declares unused external parameter entities for
    the JATS article and BITS book schemas. Remove only that exact declaration
    from a temporary validation view. Never fetch a DTD, expand entities, alter
    the saved original, or relax the separate subscription validator.
    """
    if not isinstance(payload, bytes) or not payload or len(payload) > MAX_JATS_BYTES:
        raise AdapterFailure('INVALID_JATS', 'JATS_SIZE_OR_EMPTY')
    try:
        text = payload.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise AdapterFailure('INVALID_JATS', 'JATS_XML_ENCODING_UNSUPPORTED') from None
    start = text.find('<!DOCTYPE')
    if start >= 0:
        end = text.find(']>', start)
        prefix = r'\s*(?:<\?xml\s+[^?]{1,200}\?>\s*)?(?:<\?xml-stylesheet\s+[^?]{1,500}\?>\s*)?'
        declaration = (
            r'<!DOCTYPE\s+response\s*\[\s*'
            r'<!ENTITY\s+%\s+article\s+SYSTEM\s+"http://jats\.nlm\.nih\.gov/archiving/1\.2/JATS-archivearticle1\.dtd"\s*>\s*'
            r'<!ENTITY\s+%\s+book-part-wrapper\s+SYSTEM\s+"http://jats\.nlm\.nih\.gov/extensions/bits/2\.0/BITS-book2\.dtd"\s*>\s*\]>'
        )
        if (end < 0 or end - start > 1024
                or re.fullmatch(prefix, text[:start]) is None
                or re.fullmatch(declaration, text[start:end + 2]) is None):
            raise AdapterFailure('INVALID_JATS', 'JATS_XML_SECURITY_REJECTED')
        text = text[:start] + text[end + 2:]
    view = text.encode('utf-8')
    _validate_jats_article(view, requested_doi)
    return view


class SpringerNatureAdapter(PublisherAdapter):
    adapter_ids = ("springer_nature", "springer")
    source_id = "springer-nature-fulltext-jats"
    subscription = True
    api_domain = API_DOMAIN
    # This remains a PDF manual stub. The documented endpoint returns JATS XML.
    manual_only_code = "ENDPOINT_DOCUMENTATION_REQUIRED"

    def _prepare_source(
        self,
        plan: Mapping[str, Any],
        paper: Any,
        doi: str,
        noncommercial_research: bool,
        permission_evidence: str | None,
    ):
        raise AdapterFailure("manual", "ENDPOINT_DOCUMENTATION_REQUIRED")

    def _request_headers(self, credential: str | None) -> dict[str, str]:
        return {}

    @staticmethod
    def _valid_key_metric(value: str) -> bool:
        if value.count("/") != 1:
            return False
        key, metric = value.split("/", 1)
        return all(
            part
            and part == part.strip()
            and all(0x21 <= ord(char) <= 0x7E for char in part)
            for part in (key, metric)
        )

    def _jats_policy(self, doi: str) -> tuple[str, str, str, dict[str, int]]:
        entry = self.registry_entry
        if not isinstance(entry, Mapping):
            raise AdapterFailure("manual", "REGISTRY_ENTRY_INVALID")
        if entry.get("license_status") != "approved":
            raise AdapterFailure("manual", "PUBLISHER_NOT_APPROVED")

        permission = next(
            (_clean_public_reference(value) for value in _values(entry.get("permission_evidence"))
             if _clean_public_reference(value) is not None),
            None,
        )
        if permission is None:
            raise AdapterFailure("manual", "PERMISSION_EVIDENCE_MISSING")
        if urlsplit(permission).hostname == "dev.springernature.com":
            raise AdapterFailure("manual", "PERMISSION_EVIDENCE_NOT_SPECIAL_AGREEMENT")

        if self.api_domain not in {
            str(value).lower().rstrip(".") for value in _values(entry.get("api_domains"))
        }:
            raise AdapterFailure("manual", "REGISTRY_ENDPOINT_MISMATCH")

        if not doi.startswith(("10.1007/", "10.1038/", "10.1186/")):
            raise AdapterFailure("manual", "DOI_PUBLISHER_MISMATCH")
        if entry.get("doi_prefixes") is not None and not _safe_registry_prefixes(entry, doi):
            raise AdapterFailure("manual", "REGISTRY_DOI_MISMATCH")

        publisher_names = {
            "".join(char.lower() for char in str(value) if char.isalnum())
            for value in _values(entry.get("publisher_names"))
        }
        accepted_names = {"springernature", "springer", "naturepublishinggroup", "biomedcentral"}
        if not publisher_names.intersection(accepted_names):
            raise AdapterFailure("manual", "REGISTRY_PUBLISHER_MISMATCH")
        if entry.get("adapter") is not None and entry.get("adapter") not in self.adapter_ids:
            raise AdapterFailure("manual", "REGISTRY_ADAPTER_MISMATCH")

        if entry.get("jats_product_verified") is not True:
            raise AdapterFailure("manual", "JATS_PRODUCT_UNVERIFIED")
        product_evidence = next(
            (_clean_public_reference(value) for value in _values(entry.get("jats_product_evidence"))
             if _clean_public_reference(value) is not None),
            None,
        )
        if product_evidence not in {FULLTEXT_DOCUMENTATION, MIGRATION_DOCUMENTATION}:
            raise AdapterFailure("manual", "JATS_PRODUCT_EVIDENCE_INVALID")

        credential_env = entry.get("credential_env")
        if credential_env != "SPRINGER_NATURE_API_KEY":
            raise AdapterFailure("manual", "CREDENTIAL_POLICY_INVALID")
        return credential_env, permission, product_evidence, self._validated_rate_limits(entry)

    @staticmethod
    def _artifact_paths(directory: Path, doi: str) -> tuple[Path, Path]:
        key = hashlib.sha256(doi.encode("ascii")).hexdigest()
        xml_path = directory / f"doi-{key}.jats.xml"
        return xml_path, Path(str(xml_path) + ".provenance.json")

    def _jats_result(
        self,
        status: str,
        code: str | None,
        doi: str | None,
        *,
        xml_path: Path | None = None,
        digest: str | None = None,
        eligibility: str | None = None,
        network_requests: int = 0,
        credential_reads: int = 0,
        artifact_written: bool = False,
    ) -> dict[str, Any]:
        result = {
            "status": status,
            "error": code,
            "publiccode": code,
            "resource_type": "jats_xml",
            "artifact_role": "auxiliary_fulltext_xml",
            "main_pdf_required": True,
            "main_pdf_ready": False,
            "source": self.source_id,
            "doi": doi,
            "xml_path": str(xml_path.resolve()) if xml_path is not None else None,
            "sha256": digest,
            "pdf_status": "not_provided",
            "network_requests": network_requests,
            "credential_reads": credential_reads,
            "artifact_written": artifact_written,
        }
        if eligibility is not None:
            result["eligibility"] = eligibility
        return result

    def _verify_existing(
        self,
        xml_path: Path,
        sidecar_path: Path,
        doi: str,
        permission_evidence: str,
        product_evidence: str,
    ) -> tuple[str, str] | None:
        if not xml_path.exists() and not sidecar_path.exists():
            return None
        if (not xml_path.exists() or not sidecar_path.exists()
                or xml_path.is_symlink() or sidecar_path.is_symlink()
                or not xml_path.is_file() or not sidecar_path.is_file()):
            raise AdapterFailure("NEEDS_REVIEW", "JATS_ARTIFACT_CONFLICT")
        try:
            if xml_path.stat().st_size > MAX_JATS_BYTES or sidecar_path.stat().st_size > 64 * 1024:
                raise AdapterFailure("NEEDS_REVIEW", "JATS_ARTIFACT_CONFLICT")
            payload = xml_path.read_bytes()
            record = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except AdapterFailure:
            raise
        except Exception:
            raise AdapterFailure("NEEDS_REVIEW", "JATS_ARTIFACT_CONFLICT") from None
        digest = hashlib.sha256(payload).hexdigest()
        if (not isinstance(record, dict) or record.get("version") != 1
                or record.get("resource_type") != "jats_xml"
                or record.get("doi") != doi or record.get("source") != self.source_id
                or record.get("permission_evidence") != permission_evidence
                or record.get("jats_product_evidence") != product_evidence
                or record.get("endpoint") != FULLTEXT_ENDPOINT
                or record.get("sha256") != digest
                or record.get("size_bytes") != len(payload)):
            raise AdapterFailure("NEEDS_REVIEW", "JATS_ARTIFACT_CONFLICT")
        _validate_jats_article(payload, doi)
        return digest, str(xml_path.resolve())

    def fetch_jats(
        self,
        doi: str,
        outdir: str | Path,
        governor: Any = None,
        *,
        execute: bool = False,
        confirmed: bool = False,
        noncommercial_research: bool = False,
    ) -> dict[str, Any]:
        """Preview by default; fetch one verified article only after explicit gates."""

        try:
            normalized_doi = _normalize_doi(doi)
        except (TypeError, ValueError):
            return self._jats_result("manual", "DOI_INVALID", None)
        if type(execute) is not bool or type(confirmed) is not bool or type(noncommercial_research) is not bool:
            return self._jats_result("manual", "EXECUTION_FLAGS_INVALID", normalized_doi)

        if not execute:
            try:
                _, _, _, limits = self._jats_policy(normalized_doi)
                if noncommercial_research is not True:
                    raise AdapterFailure("manual", "NONCOMMERCIAL_RESEARCH_REQUIRED")
                if governor is not None and not self._governor_policy_ok(governor, limits):
                    raise AdapterFailure("manual", "RATE_POLICY_MISMATCH")
                code = None
            except AdapterFailure as failure:
                code = failure.code
            return self._jats_result(
                "preview", code, normalized_doi,
                eligibility="blocked" if code else "eligible",
            )

        if confirmed is not True:
            return self._jats_result("manual", "EXPLICIT_SINGLE_PAPER_CONFIRMATION_REQUIRED", normalized_doi)
        if noncommercial_research is not True:
            return self._jats_result("manual", "NONCOMMERCIAL_RESEARCH_REQUIRED", normalized_doi)
        try:
            credential_env, permission_evidence, product_evidence, limits = self._jats_policy(normalized_doi)
        except AdapterFailure as failure:
            return self._jats_result(failure.status, failure.code, normalized_doi)
        except Exception:
            return self._jats_result("NEEDS_REVIEW", "JATS_POLICY_NEEDS_REVIEW", normalized_doi)

        try:
            directory = Path(outdir).expanduser().resolve()
            xml_path, sidecar_path = self._artifact_paths(directory, normalized_doi)
        except Exception:
            return self._jats_result("NEEDS_REVIEW", "OUTPUT_PATH_INVALID", normalized_doi)

        try:
            with process_lock(GLOBAL_PROCESS_LOCK):
                try:
                    existing = self._verify_existing(
                        xml_path,
                        sidecar_path,
                        normalized_doi,
                        permission_evidence,
                        product_evidence,
                    ) if directory.exists() else None
                except AdapterFailure as failure:
                    return self._jats_result(failure.status, failure.code, normalized_doi)
                if existing is not None:
                    digest, path = existing
                    return self._jats_result(
                        "exists_xml", "XML_ALREADY_VERIFIED", normalized_doi,
                        xml_path=Path(path), digest=digest,
                    )

                if governor is None:
                    try:
                        project_root = Path(__file__).resolve().parents[1]
                        governor = RequestGovernor(
                            project_root / "download_state.json",
                            interval_seconds=10,
                            max_requests_per_minute=6,
                        )
                    except Exception:
                        return self._jats_result("NEEDS_REVIEW", "GUARD_STATE_UNAVAILABLE", normalized_doi)
                if not self._governor_policy_ok(governor, limits):
                    return self._jats_result("manual", "RATE_POLICY_MISMATCH", normalized_doi)

                credential = self._read_credential(credential_env)
                if credential is None:
                    return self._jats_result(
                        "AUTH_REQUIRED", "CREDENTIAL_MISSING", normalized_doi,
                        credential_reads=1,
                    )
                if not self._valid_key_metric(credential):
                    return self._jats_result(
                        "AUTH_REQUIRED", "CREDENTIAL_FORMAT_INVALID", normalized_doi,
                        credential_reads=1,
                    )

                query = urlencode(
                    {"q": f"doi:{normalized_doi}", "api_key": credential},
                    safe="/:",
                )
                request_url = f"{FULLTEXT_ENDPOINT}?{query}"
                if _validated_https_url(
                    request_url,
                    allowed_hosts={API_DOMAIN},
                    allow_query=True,
                    allowed_query=query,
                ) is None:
                    return self._jats_result(
                        "manual", "REQUEST_URL_REJECTED", normalized_doi, credential_reads=1,
                    )

                try:
                    reserve_daily_request(directory, SAFE_API_ORIGIN, limits["per_day"])
                except GuardError:
                    return self._jats_result(
                        "RATE_LIMITED", "RATE_LIMITED", normalized_doi, credential_reads=1,
                    )
                except Exception:
                    return self._jats_result(
                        "NEEDS_REVIEW", "DAILY_REQUEST_BUDGET_REQUIRED", normalized_doi,
                        credential_reads=1,
                    )
                try:
                    governor.acquire(SAFE_API_ORIGIN, download_attempt=True)
                except GuardError as failure:
                    safe_status = getattr(failure, "status", None)
                    if safe_status not in {
                        "AUTH_EXPIRED", "ACCESS_DENIED", "DOMAIN_PAUSED", "RATE_LIMITED", "RETRY_WAIT"
                    }:
                        safe_status = "RATE_LIMITED"
                    if safe_status == "AUTH_EXPIRED":
                        safe_status = "AUTH_REQUIRED"
                    return self._jats_result(
                        safe_status, safe_status, normalized_doi, credential_reads=1,
                    )
                except Exception:
                    return self._jats_result(
                        "NEEDS_REVIEW", "GOVERNOR_ACQUIRE_FAILED", normalized_doi,
                        credential_reads=1,
                    )

                response = None
                try:
                    try:
                        response = self.transport.get(
                            request_url,
                            headers={"Accept": "application/xml"},
                            timeout=30,
                            allow_redirects=False,
                        )
                    except Exception:
                        return self._jats_result(
                            "FAILED", "TRANSPORT_ERROR", normalized_doi,
                            credential_reads=1, network_requests=1,
                        )

                    status = self._response_status(response)
                    if status is None:
                        return self._jats_result(
                            "FAILED", "TRANSPORT_RESPONSE_INVALID", normalized_doi,
                            credential_reads=1, network_requests=1,
                        )
                    if status in {301, 302, 303, 307, 308}:
                        return self._jats_result(
                            "manual", "REDIRECT_BLOCKED", normalized_doi,
                            credential_reads=1, network_requests=1,
                        )
                    if status != 200:
                        safe_status = self._http_error(
                            governor, SAFE_API_ORIGIN, status, getattr(response, "headers", {})
                        )
                        code = f"HTTP_{status}" if safe_status == "manual" else safe_status
                        return self._jats_result(
                            safe_status, code, normalized_doi,
                            credential_reads=1, network_requests=1,
                        )

                    try:
                        governor.record_success(SAFE_API_ORIGIN)
                    except Exception:
                        return self._jats_result(
                            "NEEDS_REVIEW", "GOVERNOR_STATE_FAILED", normalized_doi,
                            credential_reads=1, network_requests=1,
                        )
                    media_type = _response_content_type(response)
                    if media_type is not None and not (
                        media_type in {"application/xml", "text/xml"} or media_type.endswith("+xml")
                    ):
                        return self._jats_result(
                            "INVALID_JATS", "JATS_CONTENT_TYPE_INVALID", normalized_doi,
                            credential_reads=1, network_requests=1,
                        )
                    try:
                        payload = _read_jats_response(response)
                        if any(token in payload for token in _credential_echo_tokens(credential)):
                            raise AdapterFailure("INVALID_JATS", "JATS_RESPONSE_CONTAINS_CREDENTIAL")
                        _validate_jats_article(payload, normalized_doi)
                    except AdapterFailure as failure:
                        return self._jats_result(
                            failure.status, failure.code, normalized_doi,
                            credential_reads=1, network_requests=1,
                        )
                    except Exception:
                        return self._jats_result(
                            "INVALID_JATS", "JATS_XML_INVALID", normalized_doi,
                            credential_reads=1, network_requests=1,
                        )
                finally:
                    close = getattr(response, "close", None)
                    if callable(close):
                        try:
                            close()
                        except Exception:
                            pass

                digest = hashlib.sha256(payload).hexdigest()
                record = {
                    "version": 1,
                    "resource_type": "jats_xml",
                    "doi": normalized_doi,
                    "source": self.source_id,
                    "endpoint": FULLTEXT_ENDPOINT,
                    "permission_evidence": permission_evidence,
                    "jats_product_evidence": product_evidence,
                    "sha256": digest,
                    "size_bytes": len(payload),
                }
                try:
                    _write_atomic_new(xml_path, payload)
                    _write_json_new(sidecar_path, record)
                except AdapterFailure:
                    return self._jats_result(
                        "NEEDS_REVIEW", "JATS_PROVENANCE_WRITE_FAILED", normalized_doi,
                        xml_path=xml_path if xml_path.exists() else None,
                        digest=digest,
                        credential_reads=1,
                        network_requests=1,
                        artifact_written=xml_path.exists(),
                    )
                try:
                    verified = self._verify_existing(
                        xml_path,
                        sidecar_path,
                        normalized_doi,
                        permission_evidence,
                        product_evidence,
                    )
                except AdapterFailure:
                    verified = None
                if verified != (digest, str(xml_path.resolve())):
                    return self._jats_result(
                        "NEEDS_REVIEW", "JATS_PROVENANCE_VERIFY_FAILED", normalized_doi,
                        xml_path=xml_path,
                        digest=digest,
                        credential_reads=1,
                        network_requests=1,
                        artifact_written=True,
                    )
                return self._jats_result(
                    "downloaded_xml", None, normalized_doi,
                    xml_path=xml_path,
                    digest=digest,
                    credential_reads=1,
                    network_requests=1,
                    artifact_written=True,
                )
        except DownloadInUse:
            return self._jats_result("manual", "DOWNLOAD_ALREADY_IN_PROGRESS", normalized_doi)
        except Exception:
            return self._jats_result("NEEDS_REVIEW", "ADAPTER_STATE_NEEDS_REVIEW", normalized_doi)
