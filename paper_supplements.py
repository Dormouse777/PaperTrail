"""Acquire and verify article supplementary files into per-paper bundles.

Automatic discovery is limited to an explicitly eligible PMC OA record. Direct
publisher links are accepted only when supplied as confirmed, public records.
Nothing in this module opens or executes acquired files.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import unicodedata
import zipfile
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlsplit

from download_guard import GuardError, RequestGovernor
from paper_bundle import BundleError, file_identity, read_bundle, verified_supplements, write_bundle
from download_ledger import identity_keys, normalized_doi
from process_lock import process_lock


ROOT = Path(__file__).resolve().parent
GLOBAL_PROCESS_LOCK = ROOT / "paper_downloader-global"
PMC_SUPPLEMENT_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/supplementaryFiles"
MAX_SINGLE_FILE_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_BUNDLE_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 1024
MAX_INPUT_RECORDS = 32
MAX_COMPRESSION_RATIO = 500
CHUNK_SIZE = 128 * 1024
PMCID_RE = re.compile(r"PMC[1-9][0-9]{0,15}\Z", re.IGNORECASE)
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

_MIME_BY_EXTENSION = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".zip": "application/zip",
    ".mp4": "video/mp4",
    ".m4v": "video/x-m4v",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".png": "image/png",
}
_GENERIC_MIMES = {"application/octet-stream", "binary/octet-stream", "application/binary", ""}
_ALLOWED_MIME_ALIASES = {
    ".docx": {"application/zip", "application/x-zip-compressed"},
    ".xlsx": {"application/zip", "application/x-zip-compressed"},
    ".zip": {"application/x-zip-compressed", "application/x-zip"},
    ".doc": {"application/x-ole-storage"},
}


class _SupplementFailure(ValueError):
    def __init__(self, code: str, status: str = "partial"):
        self.code = code
        self.status = status
        super().__init__(code)


def _regular_chain(path: Path) -> None:
    for node in (path, *path.parents):
        try:
            info = node.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
            raise BundleError("BUNDLE_LINK_PATH_REJECTED")


def _valid_license(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 512 or any(ord(c) < 32 for c in value):
        return False
    normalized = value.strip().lower().rstrip("/")
    prefix = "https://creativecommons.org/licenses/"
    if normalized.startswith(prefix):
        normalized = "cc-" + normalized[len(prefix):]
    elif normalized.startswith("http://creativecommons.org/licenses/"):
        normalized = "cc-" + normalized[len("http://creativecommons.org/licenses/"):]
    elif normalized.startswith("https://creativecommons.org/publicdomain/zero/"):
        version = normalized.rsplit("/", 1)[-1]
        normalized = "cc0-" + version
    elif normalized.startswith("https://creativecommons.org/publicdomain/mark/"):
        normalized = "public-domain-mark"
    normalized = normalized.strip(" /").replace(" ", "-").replace("_", "-")
    normalized = re.sub(r"/", "-", normalized)
    if normalized in {"public-domain", "public-domain-mark", "cc0", "cc0-1.0", "cc-zero"}:
        return True
    return re.fullmatch(
        r"cc-(?:by(?:-(?:nc(?:-(?:nd|sa))?|nd(?:-sa)?|sa))?)"
        r"(?:-(?:1(?:\.0)?|2(?:\.0|\.5)?|3(?:\.0)?|4(?:\.0)?))?",
        normalized,
    ) is not None


def pmc_supplements_eligible(paper: Any) -> bool:
    """Require a PMC id, affirmative official OA observation, and known license."""
    if not isinstance(paper, Mapping) or paper.get("source") not in ("pubmed", "zotero"):
        return False
    if paper.get("source") == "zotero":
        # Existing Zotero bundles retain their identity. A separate official
        # DOI/PMCID/OA record is required instead of relabelling them PubMed.
        binding = paper.get("pmc_identity_evidence", {})
        try:
            evidence_path = Path(binding["path"])
            raw = evidence_path.read_bytes()
            import hashlib
            if hashlib.sha256(raw).hexdigest() != binding["sha256"]:
                return False
            evidence = json.loads(raw)
            if (evidence.get("status") != 200
                    or not evidence.get("url", "").startswith("https://www.ebi.ac.uk/europepmc/webservices/rest/search?")
                    or hashlib.sha256(evidence["body"].encode()).hexdigest() != evidence.get("body_sha256")):
                return False
            records = json.loads(evidence["body"])["resultList"]["result"]
            matching = [r for r in records if r.get("doi", "").lower() == paper.get("doi", "").lower()
                and r.get("pmcid") == paper.get("pmcid") and r.get("isOpenAccess") == "Y"
                and r.get("license") == paper.get("supplements_license")]
            if len(matching) != 1:
                return False
        except (OSError, ValueError, KeyError, TypeError):
            return False
    pmcid = paper.get("pmcid")
    return (isinstance(pmcid, str) and PMCID_RE.fullmatch(pmcid) is not None
            and paper.get("supplements_oa_eligible") is True
            and _valid_license(paper.get("supplements_license")))


def _bound_paper(supplied: Mapping[str, Any], bundle_paper: Mapping[str, Any]) -> dict[str, Any]:
    """Bind optional supplement input to the identity already saved for this PDF."""
    supplied_identity = {key: supplied[key] for key in ("doi", "pmcid", "source", "id")
                         if supplied.get(key) is not None}
    saved_identity = {key: bundle_paper[key] for key in ("doi", "pmcid", "source", "id")
                      if bundle_paper.get(key) is not None}
    if supplied_identity and not set(identity_keys(supplied_identity)).intersection(identity_keys(saved_identity)):
        raise BundleError("BUNDLE_PAPER_IDENTITY_CONFLICT")
    for key in ("doi", "pmcid", "source", "id"):
        incoming, saved = supplied.get(key), bundle_paper.get(key)
        if incoming is None:
            continue
        if saved is None:
            # An otherwise matching DOI must not bless an unbound PMCID (or
            # other identifier) that could point at a different article.
            raise BundleError("BUNDLE_PAPER_IDENTITY_CONFLICT")
        if key == "doi":
            same = normalized_doi(incoming) is not None and normalized_doi(incoming) == normalized_doi(saved)
        elif key == "pmcid":
            same = (isinstance(incoming, str) and isinstance(saved, str)
                    and PMCID_RE.fullmatch(incoming) is not None
                    and PMCID_RE.fullmatch(saved) is not None
                    and incoming.upper() == saved.upper())
        else:
            same = str(incoming).strip().casefold() == str(saved).strip().casefold()
        if not same:
            raise BundleError("BUNDLE_PAPER_IDENTITY_CONFLICT")
    result = dict(bundle_paper)
    for key in ("supplements", "supplements_oa_eligible", "supplements_license"):
        if key in supplied:
            if key != "supplements" and key in bundle_paper and bundle_paper[key] != supplied[key]:
                raise BundleError("BUNDLE_SUPPLEMENT_METADATA_CONFLICT")
            result[key] = supplied[key]
    return result


def _safe_filename(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 240:
        return None
    value = unicodedata.normalize("NFC", value)
    if (value in {".", ".."} or value.startswith(".") or value.endswith((".", " "))
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or any(c in value for c in '<>:"/\\|?*')):
        return None
    if value.split(".", 1)[0].upper() in {
        "CON", "PRN", "AUX", "NUL", *("COM" + str(i) for i in range(1, 10)),
        *("LPT" + str(i) for i in range(1, 10)),
    }:
        return None
    return value


def _safe_source(value: Any, host: str) -> str:
    # Saved manifest inputs already carry this namespace. Keep their identity
    # stable on CLI resume instead of replacing it with the download hostname.
    if isinstance(value, str) and value.startswith("supplement:"):
        value = value[len("supplement:"):]
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value):
        return "supplement:" + value.lower()
    return "supplement:" + host.lower()


def _validate_public_supplement_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.isascii() or len(value) > 8192:
        return None
    if any(ord(c) <= 32 or ord(c) == 127 for c in value) or "\\" in value or "#" in value:
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname.lower() if parsed.hostname else ""
        if (parsed.scheme.lower() != "https" or not host or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)
                or parsed.fragment or parsed.query not in ("", "download=1", "download=true", "pdf=render")):
            return None
        from fulltext_url import validated_public_fulltext_url
        return value if validated_public_fulltext_url(value) is not None else None
    except (ValueError, UnicodeError):
        return None


def _manual_inputs(paper: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw = paper.get("supplements")
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], [{"status": "failed", "error": "SUPPLEMENT_RECORDS_INVALID"}]
    if len(raw) > MAX_INPUT_RECORDS:
        return [], [{"status": "failed", "error": "SUPPLEMENT_RECORD_LIMIT_EXCEEDED"}]
    accepted, rejected = [], []
    from publishers.base import _clean_public_reference
    for record in raw:
        if not isinstance(record, dict):
            rejected.append({"status": "failed", "error": "SUPPLEMENT_RECORD_INVALID"})
            continue
        filename = _safe_filename(record.get("filename"))
        url = _validate_public_supplement_url(record.get("url"))
        evidence = _clean_public_reference(record.get("permission_evidence"))
        license_value = record.get("license")
        if (record.get("confirmed") is not True or filename is None or url is None or evidence is None
                or (license_value is not None and (not isinstance(license_value, str)
                    or len(license_value) > 512 or any(ord(c) < 32 for c in license_value)))):
            rejected.append({"status": "failed", "filename": filename,
                             "error": "SUPPLEMENT_RECORD_REQUIRES_CONFIRMED_PUBLIC_LINK"})
            continue
        extension = Path(filename).suffix.lower()
        if extension not in _MIME_BY_EXTENSION:
            rejected.append({"status": "failed", "filename": filename,
                             "error": "SUPPLEMENT_FORMAT_UNSUPPORTED"})
            continue
        host = urlsplit(url).hostname.lower()
        source = _safe_source(record.get("source"), host)
        accepted.append({"url": url, "filename": filename,
                         "permission_evidence": evidence, "confirmed": True,
                         "license": license_value or "user-confirmed-access-not-oa",
                         "source": source})
    return accepted, rejected


def _governor_policy_ok(governor: Any) -> bool:
    if not all(callable(getattr(governor, name, None)) for name in
               ("acquire", "record_http_error", "record_success")):
        return False
    try:
        # Accept the application's own RequestGovernor defaults (5 seconds,
        # 12/minute) while rejecting looser injected policies.
        return float(governor.interval) >= 3 and 1 <= int(governor.per_minute) <= 12
    except (TypeError, ValueError, AttributeError):
        return False


def _header(response: Any, name: str) -> str | None:
    headers = getattr(response, "headers", {})
    try:
        value = headers.get(name) if hasattr(headers, "get") else None
    except Exception:
        return None
    return value if isinstance(value, str) else None


def _http_failure(url: str, status: int, headers: Any, governor: Any) -> str:
    if status in {401, 403, 406, 429} or 500 <= status <= 599:
        safe_status = status if status in {401, 403, 406, 429, 500, 502, 503, 504} else 500
        try:
            failure = governor.record_http_error(url, safe_status,
                headers.get("Retry-After") if hasattr(headers, "get") else None,
                authenticated=False)
        except Exception:
            return "SUPPLEMENT_GOVERNOR_STATE_FAILED"
        if status == 401:
            return "AUTH_REQUIRED"
        if status == 403:
            return "ACCESS_DENIED"
        state = getattr(failure, "status", None)
        return state if state in {"DOMAIN_PAUSED", "RETRY_WAIT", "RATE_LIMITED"} else "RATE_LIMITED"
    if status in {301, 302, 303, 307, 308}:
        return "SUPPLEMENT_REDIRECT_BLOCKED"
    return "HTTP_" + str(status)


def _content_type_allowed(extension: str, raw: str | None, *, archive: bool = False) -> bool:
    mime = (raw or "").split(";", 1)[0].strip().lower()
    if archive:
        return mime in _GENERIC_MIMES | {"application/zip", "application/x-zip", "application/x-zip-compressed"}
    expected = _MIME_BY_EXTENSION.get(extension)
    return mime in _GENERIC_MIMES or mime == expected or mime in _ALLOWED_MIME_ALIASES.get(extension, set())


def _download_temp(url: str, *, temp_dir: Path, governor: Any, transport: Any,
                   budget_dir: Path, limit: int, extension: str = ".zip",
                   archive: bool = False) -> tuple[Path, str | None]:
    if not _governor_policy_ok(governor):
        raise _SupplementFailure("SUPPLEMENT_RATE_POLICY_INVALID")
    temp_path = None
    response = None
    try:
        from publishers.base import DailyRequestBudgetError, reserve_daily_request
        try:
            reserve_daily_request(budget_dir, url, 100)
        except DailyRequestBudgetError as failure:
            code = ("RATE_LIMITED" if failure.code == "DAILY_REQUEST_BUDGET_EXHAUSTED"
                    else "SUPPLEMENT_BUDGET_STATE_FAILED")
            raise _SupplementFailure(code) from None
        governor.acquire(url, download_attempt=True)
        response = transport.get(url, headers={"Accept": "application/zip, application/octet-stream, */*"},
                                 timeout=60, allow_redirects=False)
        status = getattr(response, "status", getattr(response, "status_code", getattr(response, "code", None)))
        if type(status) is not int:
            raise _SupplementFailure("SUPPLEMENT_RESPONSE_INVALID")
        if status != 200:
            raise _SupplementFailure(_http_failure(url, status, getattr(response, "headers", {}), governor))
        try:
            governor.record_success(url)
        except Exception:
            raise _SupplementFailure("SUPPLEMENT_GOVERNOR_STATE_FAILED") from None
        raw_length = _header(response, "Content-Length")
        if raw_length is not None:
            try:
                length = int(raw_length)
            except ValueError:
                length = None
            if length is not None and (length < 0 or length > limit):
                raise _SupplementFailure("SUPPLEMENT_SIZE_LIMIT_EXCEEDED")
        fd, raw_path = tempfile.mkstemp(prefix=".supplement-", suffix=".partial", dir=temp_dir)
        temp_path = Path(raw_path)
        total = 0
        reader = getattr(response, "read", None)
        if not callable(reader):
            raise _SupplementFailure("SUPPLEMENT_RESPONSE_INVALID")
        with os.fdopen(fd, "wb") as stream:
            while True:
                block = reader(CHUNK_SIZE)
                if not block:
                    break
                if not isinstance(block, (bytes, bytearray)):
                    raise _SupplementFailure("SUPPLEMENT_RESPONSE_INVALID")
                total += len(block)
                if total > limit:
                    raise _SupplementFailure("SUPPLEMENT_SIZE_LIMIT_EXCEEDED")
                stream.write(block)
            stream.flush()
            os.fsync(stream.fileno())
        if total <= 0:
            raise _SupplementFailure("SUPPLEMENT_FILE_EMPTY")
        raw_mime = _header(response, "Content-Type")
        if not _content_type_allowed(extension, raw_mime, archive=archive):
            raise _SupplementFailure("SUPPLEMENT_MIME_INVALID")
        if archive:
            with temp_path.open("rb") as stream:
                if stream.read(4) not in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
                    raise _SupplementFailure("SUPPLEMENT_NOT_ZIP")
        return temp_path, raw_mime
    except _SupplementFailure:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise
    except GuardError as failure:
        code = getattr(failure, "status", "RATE_LIMITED")
        if code not in {"AUTH_EXPIRED", "ACCESS_DENIED", "DOMAIN_PAUSED", "RATE_LIMITED", "RETRY_WAIT"}:
            code = "RATE_LIMITED"
        if code == "AUTH_EXPIRED":
            code = "AUTH_REQUIRED"
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise _SupplementFailure(code) from None
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise _SupplementFailure("SUPPLEMENT_TRANSPORT_FAILED") from None
    finally:
        if response is not None:
            close = getattr(response, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass


def _zip_entry_name(name: str) -> str | None:
    if (not isinstance(name, str) or not name or "\\" in name or "\x00" in name
            or name.startswith("/") or re.match(r"^[A-Za-z]:", name)):
        return None
    raw_parts = name.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts) or PurePosixPath(name).is_absolute():
        return None
    return raw_parts[-1] if raw_parts else None


def _zip_mode_is_safe(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    kind = stat.S_IFMT(mode)
    return kind in {0, stat.S_IFREG}


def _is_pmc_article_figure_rendition(filename: str) -> bool:
    """Identify known PMC ZIP renditions of article-page figures, not SI files.

    Europe PMC's supplementaryFiles ZIP can contain HTML-viewer images for the
    main article alongside the actual supplementary material. Keep this
    deliberately narrow: names explicitly used by PMC/JATS figure renderers
    are excluded, while numbered supplement assets such as ``s001.tif`` remain
    eligible for validation and filing.
    """
    stem = Path(filename).stem
    patterns = (
        r".+_Fig\d+_HTML",       # Nature HTML-viewer copies
        r"(?:gr|ga)\d+",          # Graphical-abstract/figure renditions
        r".+g\d{3,}",             # JATS body figures (often -g001, sometimes g001)
        r".+fig\d{1,3}",         # PNAS body-figure renditions
        r".+-(?:f|t)\d{1,3}",    # PMC's RSC-style main-text figure/table copies
        r".+-ga",                  # PMC's RSC-style graphical-abstract copy
        r"nihpp-\d{4}\.\d{2}\.\d{2}\.\d{6,}v\d+-f\d{4}",  # PMC preprint body figures
    )
    return any(re.fullmatch(pattern, stem, re.IGNORECASE) for pattern in patterns)


def _archive_pmc_figure_rendition(record: Mapping[str, Any], *, pdf: Path,
                                  manifest: dict[str, Any], verified: list[dict[str, Any]]) -> dict[str, Any]:
    """Remove a previously accepted PMC body-figure copy from active supplements.

    Preserve the bytes under an audit folder and bind the move to their recorded
    hash. This is a local-only reconciliation; it never refetches the PMC ZIP.
    """
    filename = record.get("filename")
    if not isinstance(filename, str) or _safe_filename(filename) is None:
        raise BundleError("PMC_FIGURE_RENDITION_RECORD_INVALID")
    relative = record.get("relative_path")
    result = {"status": "excluded", "filename": filename,
        "source": record.get("source", "europepmc:supplementaryFiles"),
        "reason_code": "PMC_MAIN_ARTICLE_FIGURE_RENDITION",
        "previous_status": record.get("status")}
    if not isinstance(relative, str) or relative != "supplements/" + filename:
        return result
    source = pdf.parent / relative
    _regular_chain(source)
    if not source.exists():
        result["previous_relative_path"] = relative
        return result
    identity = file_identity(source)
    if (record.get("sha256") != identity["sha256"]
            or record.get("size_bytes") != identity["size_bytes"]):
        raise BundleError("PMC_FIGURE_RENDITION_HASH_CHANGED")
    archive_dir = pdf.parent / "excluded-pmc-figure-renditions"
    _regular_chain(archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)
    _regular_chain(archive_dir)
    destination = archive_dir / filename
    _regular_chain(destination)
    if destination.exists():
        archived_identity = file_identity(destination)
        if archived_identity["sha256"] != identity["sha256"]:
            raise BundleError("PMC_FIGURE_RENDITION_FILENAME_COLLISION")
        source.unlink()
    else:
        os.replace(source, destination)
    result.update({"previous_relative_path": relative,
        "archived_relative_path": "excluded-pmc-figure-renditions/" + filename,
        "sha256": identity["sha256"], "size_bytes": identity["size_bytes"]})
    manifest["files"] = [item for item in manifest.get("files", [])
        if not (isinstance(item, dict) and item.get("relative_path") == relative
            and item.get("sha256") == identity["sha256"])]
    verified[:] = [item for item in verified
        if not (item.get("relative_path") == relative and item.get("sha256") == identity["sha256"])]
    return result


def _archive_preflight(path: Path, *, for_extract: bool) -> tuple[list[tuple[zipfile.ZipInfo, str]], list[dict[str, Any]]]:
    accepted: list[tuple[zipfile.ZipInfo, str]] = []
    failures: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(path, "r") as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_MEMBERS:
                raise _SupplementFailure("SUPPLEMENT_ARCHIVE_MEMBER_LIMIT_EXCEEDED")
            declared_total = sum(info.file_size for info in infos if not info.is_dir())
            if declared_total > MAX_BUNDLE_BYTES:
                raise _SupplementFailure("SUPPLEMENT_ARCHIVE_EXPANSION_LIMIT_EXCEEDED")
            basenames: dict[str, list[tuple[zipfile.ZipInfo, str]]] = {}
            for info in infos:
                if info.is_dir():
                    directory_name = info.filename[:-1] if info.filename.endswith("/") else info.filename
                    mode = stat.S_IFMT((info.external_attr >> 16) & 0xFFFF)
                    if (_zip_entry_name(directory_name) is None
                            or mode not in {0, stat.S_IFDIR}):
                        failures.append({"status": "failed", "filename": None,
                                         "error": "SUPPLEMENT_ARCHIVE_ENTRY_REJECTED"})
                    continue
                filename = _zip_entry_name(info.filename)
                if filename is None or not _zip_mode_is_safe(info) or info.flag_bits & 0x1:
                    failures.append({"status": "failed", "filename": None,
                                     "error": "SUPPLEMENT_ARCHIVE_ENTRY_REJECTED"})
                    continue
                if info.file_size > MAX_SINGLE_FILE_BYTES:
                    failures.append({"status": "failed", "filename": filename,
                                     "error": "SUPPLEMENT_SIZE_LIMIT_EXCEEDED"})
                    continue
                if info.file_size and (info.compress_size == 0 or
                    info.file_size > max(info.compress_size, 1) * MAX_COMPRESSION_RATIO):
                    failures.append({"status": "failed", "filename": filename,
                                     "error": "SUPPLEMENT_ARCHIVE_RATIO_LIMIT_EXCEEDED"})
                    continue
                if for_extract:
                    filename = _safe_filename(filename)
                    if filename is None:
                        failures.append({"status": "failed", "filename": None,
                                         "error": "SUPPLEMENT_FILENAME_INVALID"})
                        continue
                else:
                    # Preserved ZIP containers may legitimately contain dotfiles
                    # and repeated basenames; they are never extracted here.
                    accepted.append((info, filename))
                    continue
                if for_extract and _is_pmc_article_figure_rendition(filename):
                    failures.append({"status": "excluded", "filename": filename,
                                     "reason_code": "PMC_MAIN_ARTICLE_FIGURE_RENDITION"})
                    continue
                if Path(filename).suffix.lower() not in _MIME_BY_EXTENSION:
                    failures.append({"status": "failed", "filename": filename,
                                     "error": "SUPPLEMENT_FORMAT_UNSUPPORTED"})
                    continue
                basenames.setdefault(filename.casefold(), []).append((info, filename))
            for group in basenames.values():
                if len(group) > 1:
                    for _, filename in group:
                        failures.append({"status": "failed", "filename": filename,
                                         "error": "SUPPLEMENT_ARCHIVE_NAME_COLLISION"})
                else:
                    accepted.extend(group)
            if not for_extract:
                if failures:
                    raise _SupplementFailure("SUPPLEMENT_ARCHIVE_ENTRY_REJECTED")
                for bad in (archive.testzip(),):
                    if bad is not None:
                        raise _SupplementFailure("SUPPLEMENT_ARCHIVE_CRC_INVALID")
    except _SupplementFailure:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError, EOFError, ValueError):
        raise _SupplementFailure("SUPPLEMENT_ARCHIVE_INVALID") from None
    return accepted, failures


def _file_signature(path: Path, filename: str) -> bool:
    extension = Path(filename).suffix.lower()
    size = path.stat().st_size
    with path.open("rb") as stream:
        head = stream.read(64)
        if extension == ".pdf":
            stream.seek(max(0, size - 128))
            return head.startswith(b"%PDF-") and b"%%EOF" in stream.read(128)
        if extension in {".tif", ".tiff"}:
            return head.startswith((b"II*\x00", b"MM\x00*",
                                    b"II+\x00\x08\x00\x00\x00",
                                    b"MM\x00+\x00\x08\x00\x00"))
        if extension in {".jpg", ".jpeg"}:
            return head.startswith(b"\xff\xd8\xff")
        if extension == ".gif":
            return head.startswith((b"GIF87a", b"GIF89a"))
        if extension == ".png":
            return head.startswith(b"\x89PNG\r\n\x1a\n")
        if extension == ".doc":
            return head.startswith(bytes.fromhex("D0CF11E0A1B11AE1"))
        if extension in {".docx", ".xlsx", ".zip"}:
            if not head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
                return False
            entries, _ = _archive_preflight(path, for_extract=False)
            if extension == ".docx":
                names = {item.filename.replace("\\", "/") for item, _ in entries}
                return "[Content_Types].xml" in names and "word/document.xml" in names
            if extension == ".xlsx":
                names = {item.filename.replace("\\", "/") for item, _ in entries}
                return "[Content_Types].xml" in names and "xl/workbook.xml" in names
            return True
        if extension in {".mp4", ".m4v", ".mov"}:
            return len(head) >= 12 and head[4:8] == b"ftyp"
        if extension == ".webm":
            return head.startswith(bytes.fromhex("1A45DFA3"))
    return False


def _validate_file(path: Path, filename: str, raw_mime: str | None) -> str:
    extension = Path(filename).suffix.lower()
    if extension not in _MIME_BY_EXTENSION or path.stat().st_size <= 0:
        raise _SupplementFailure("SUPPLEMENT_FORMAT_UNSUPPORTED")
    if not _content_type_allowed(extension, raw_mime):
        raise _SupplementFailure("SUPPLEMENT_MIME_INVALID")
    if not _file_signature(path, filename):
        raise _SupplementFailure("SUPPLEMENT_FILE_SIGNATURE_INVALID")
    return _MIME_BY_EXTENSION[extension]


def _publish_temp(temp_path: Path, destination: Path) -> tuple[dict[str, Any], str]:
    _regular_chain(destination)
    identity = file_identity(temp_path)
    try:
        os.link(temp_path, destination)
        temp_path.unlink(missing_ok=True)
        return identity, "downloaded"
    except FileExistsError:
        try:
            existing = file_identity(destination)
        except Exception:
            raise _SupplementFailure("SUPPLEMENT_DESTINATION_UNVERIFIABLE") from None
        if existing["sha256"] == identity["sha256"] and existing["size_bytes"] == identity["size_bytes"]:
            temp_path.unlink(missing_ok=True)
            return existing, "exists"
        raise _SupplementFailure("SUPPLEMENT_FILENAME_COLLISION") from None
    except OSError:
        raise _SupplementFailure("SUPPLEMENT_PUBLISH_FAILED") from None


def _existing_record(verified: list[dict[str, Any]], filename: str,
                     source: str | None = None, license_value: str | None = None) -> dict[str, Any] | None:
    for item in verified:
        relative = item.get("relative_path")
        if (item["filename"].casefold() == filename.casefold()
                and isinstance(relative, str)
                and relative.casefold() == ("supplements/" + filename).casefold()):
            if source is not None and item.get("source") != source:
                continue
            if license_value is not None and item.get("license") != license_value:
                continue
            return item
    return None


def _result_from_existing(record: Mapping[str, Any], *, source: str, filename: str,
                          license_value: str, permission_evidence: str | None = None,
                          request_url: str | None = None) -> dict[str, Any]:
    result = {"status": "exists", "filename": filename,
              "relative_path": record["relative_path"], "content_type": record["content_type"],
              "size_bytes": record["size_bytes"], "sha256": record["sha256"],
              "source": source, "license": license_value}
    if permission_evidence:
        result["permission_evidence"] = permission_evidence
    if request_url:
        result["request_url"] = request_url
    return result


def _extract_pmc_zip(zip_path: Path, *, supplement_dir: Path, verified: list[dict[str, Any]],
                     license_value: str, budget_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    entries, preflight = _archive_preflight(zip_path, for_extract=True)
    exclusions = [item for item in preflight if item.get("status") == "excluded"]
    failures = [item for item in preflight if item.get("status") != "excluded"]
    results = []
    current_bytes = sum(item.get("size_bytes", 0) for item in verified)
    with zipfile.ZipFile(zip_path, "r") as archive:
        for info, filename in entries:
            extension = Path(filename).suffix.lower()
            if not _content_type_allowed(extension, None):
                failures.append({"status": "failed", "filename": filename,
                                 "error": "SUPPLEMENT_MIME_INVALID"})
                continue
            destination = supplement_dir / filename
            already_verified = _existing_record(verified, filename,
                "europepmc:supplementaryFiles", license_value)
            registered_path = _existing_record(verified, filename)
            if registered_path is not None and already_verified is None:
                failures.append({"status": "failed", "filename": filename,
                                 "error": "SUPPLEMENT_FILENAME_COLLISION"})
                continue
            if not already_verified and current_bytes + info.file_size > MAX_BUNDLE_BYTES:
                failures.append({"status": "failed", "filename": filename,
                                 "error": "SUPPLEMENT_BUNDLE_SIZE_LIMIT_EXCEEDED"})
                continue
            temp_path = None
            try:
                _regular_chain(destination)
                fd, raw = tempfile.mkstemp(prefix=".extract-", suffix=".partial", dir=supplement_dir)
                temp_path = Path(raw)
                written = 0
                with os.fdopen(fd, "wb") as stream, archive.open(info, "r") as source:
                    while True:
                        block = source.read(CHUNK_SIZE)
                        if not block:
                            break
                        written += len(block)
                        if written > MAX_SINGLE_FILE_BYTES or current_bytes + written > MAX_BUNDLE_BYTES:
                            raise _SupplementFailure("SUPPLEMENT_SIZE_LIMIT_EXCEEDED")
                        stream.write(block)
                    stream.flush()
                    os.fsync(stream.fileno())
                if written != info.file_size:
                    raise _SupplementFailure("SUPPLEMENT_ARCHIVE_MEMBER_INVALID")
                mime = _validate_file(temp_path, filename, _MIME_BY_EXTENSION[extension])
                identity, status = _publish_temp(temp_path, destination)
                temp_path = None
                file_record = {"relative_path": "supplements/" + filename, "role": "supplement",
                    "filename": filename, "content_type": mime, **identity,
                    "source": "europepmc:supplementaryFiles", "license": license_value}
                results.append({"status": status, "filename": filename,
                    "relative_path": file_record["relative_path"], "content_type": mime,
                    "size_bytes": identity["size_bytes"], "sha256": identity["sha256"],
                    "source": file_record["source"], "license": license_value})
                if status in {"downloaded", "exists"} and already_verified is None:
                    verified.append({**file_record, "path": destination})
                    current_bytes += identity["size_bytes"]
            except _SupplementFailure as failure:
                failures.append({"status": "failed", "filename": filename, "error": failure.code})
            except (OSError, RuntimeError, zipfile.BadZipFile, EOFError):
                failures.append({"status": "failed", "filename": filename,
                                 "error": "SUPPLEMENT_ARCHIVE_MEMBER_INVALID"})
            finally:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)
    return results, failures, exclusions


def _download_manual(record: Mapping[str, Any], *, supplement_dir: Path, budget_dir: Path,
                     governor: Any, transport: Any, verified: list[dict[str, Any]],
                     prior_results: list[dict[str, Any]], index: int) -> dict[str, Any]:
    filename, source, license_value = record["filename"], record["source"], record["license"]
    prior = next((item for item in prior_results if item.get("status") in {"downloaded", "exists"}
                  and item.get("request_url") == record["url"]
                  and item.get("filename") == filename), None)
    if prior:
        current = _existing_record(verified, filename, source, license_value)
        if current:
            return _result_from_existing(current, source=source, filename=filename,
                license_value=license_value, permission_evidence=record["permission_evidence"],
                request_url=record["url"])
    existing = _existing_record(verified, filename, source, license_value)
    registered_path = _existing_record(verified, filename)
    extension = Path(filename).suffix.lower()
    limit = MAX_ARCHIVE_BYTES if extension == ".zip" else MAX_SINGLE_FILE_BYTES
    temp_path, raw_mime = _download_temp(record["url"], temp_dir=supplement_dir, governor=governor,
        transport=transport, budget_dir=budget_dir, limit=limit, extension=extension)
    try:
        mime = _validate_file(temp_path, filename, raw_mime)
        current_bytes = sum(item.get("size_bytes", 0) for item in verified)
        identity = file_identity(temp_path)
        destination = supplement_dir / filename
        _regular_chain(destination)
        if existing is not None:
            if (existing.get("sha256") != identity["sha256"]
                    or existing.get("size_bytes") != identity["size_bytes"]):
                raise _SupplementFailure("SUPPLEMENT_FILENAME_COLLISION")
        elif registered_path is not None:
            raise _SupplementFailure("SUPPLEMENT_FILENAME_COLLISION")
        elif destination.exists():
            # Unknown pre-existing files are never overwritten. A byte-for-byte
            # match can be registered, but it still counts toward the bundle cap.
            try:
                on_disk = file_identity(destination)
            except Exception:
                raise _SupplementFailure("SUPPLEMENT_DESTINATION_UNVERIFIABLE") from None
            if (on_disk["sha256"] != identity["sha256"]
                    or on_disk["size_bytes"] != identity["size_bytes"]):
                raise _SupplementFailure("SUPPLEMENT_FILENAME_COLLISION")
            if current_bytes + identity["size_bytes"] > MAX_BUNDLE_BYTES:
                raise _SupplementFailure("SUPPLEMENT_BUNDLE_SIZE_LIMIT_EXCEEDED")
        elif current_bytes + identity["size_bytes"] > MAX_BUNDLE_BYTES:
            raise _SupplementFailure("SUPPLEMENT_BUNDLE_SIZE_LIMIT_EXCEEDED")
        identity, status = _publish_temp(temp_path, destination)
        temp_path = None
        return {"status": status, "filename": filename, "relative_path": "supplements/" + filename,
            "content_type": mime, "size_bytes": identity["size_bytes"], "sha256": identity["sha256"],
            "source": source, "license": license_value,
            "permission_evidence": record["permission_evidence"], "request_url": record["url"]}
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _save_manifest(pdf: Path, manifest: dict[str, Any], *, status: str,
                   results: list[dict[str, Any]], inputs: list[dict[str, Any]],
                   pmc_eligibility: dict[str, Any] | None, pmc_complete: bool) -> str:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    supplement_state = manifest.get("supplements")
    if not isinstance(supplement_state, dict):
        supplement_state = {}
    supplement_state.update({"status": status, "results": results, "inputs": inputs,
                             "updated_at_utc": now})
    if pmc_eligibility is not None:
        supplement_state["pmc_eligibility"] = pmc_eligibility
    if pmc_complete:
        supplement_state["pmc_zip_checked"] = True
    manifest["supplements"] = supplement_state
    known = {r.get("relative_path").casefold() for r in manifest.get("files", [])
             if isinstance(r, dict) and isinstance(r.get("relative_path"), str)}
    for result in results:
        if result.get("status") not in {"downloaded", "exists"} or not result.get("relative_path"):
            continue
        relative = result["relative_path"]
        if relative.casefold() in known:
            continue
        manifest["files"].append({key: result[key] for key in (
            "relative_path", "filename", "content_type", "size_bytes", "sha256", "source", "license"
        ) if key in result} | {"role": "supplement",
            **({"source_url": result["request_url"]} if result.get("request_url") else {})})
        known.add(relative.casefold())
    write_bundle(pdf.parent, manifest)
    return str(pdf.parent / "bundle.json")


def acquire_supplements(paper, pdf_path, *, governor=None, transport=None) -> dict[str, Any]:
    from acquisition_gateway import dispatch_selected
    return dispatch_selected("official_supplements", "supplement", lambda:
        _acquire_supplements_selected(paper, pdf_path, governor=governor, transport=transport))


def _acquire_supplements_selected(paper, pdf_path, *, governor=None, transport=None) -> dict[str, Any]:
    """Acquire safe supplementary files for one already-recorded paper PDF.

    The function never follows redirects and never uses PDF credentials. An
    injected transport follows the same `get(..., allow_redirects=False)`
    contract as the publisher transport.
    """
    empty = {"supplements_status": "not_checked", "supplements": [], "bundle_path": None}
    if not isinstance(paper, Mapping):
        return {**empty, "supplements_status": "partial", "supplements_error": "SUPPLEMENT_PAPER_INVALID"}
    try:
        pdf = Path(pdf_path).absolute()
        _regular_chain(pdf)
        if not pdf.is_file():
            return {**empty, "supplements_status": "partial", "supplements_error": "MAIN_PDF_MISSING"}
        bundle_path = pdf.parent / "bundle.json"
        empty["bundle_path"] = str(bundle_path)
    except (OSError, TypeError, ValueError, BundleError):
        return {**empty, "supplements_status": "partial", "supplements_error": "SUPPLEMENT_PATH_INVALID"}

    try:
        with process_lock(GLOBAL_PROCESS_LOCK):
            manifest = read_bundle(pdf.parent)
            if manifest is None:
                return {**empty, "supplements_status": "not_checked", "supplements_error": "BUNDLE_NOT_RECORDED"}
            if not isinstance(manifest.get("paper"), dict) or not isinstance(manifest.get("files"), list):
                raise BundleError("BUNDLE_MANIFEST_INVALID")
            paper = _bound_paper(paper, manifest["paper"])
            verified = verified_supplements(pdf)
            existing_bytes = sum(item["size_bytes"] for item in verified)
            if existing_bytes > MAX_BUNDLE_BYTES:
                raise _SupplementFailure("SUPPLEMENT_BUNDLE_SIZE_LIMIT_EXCEEDED")

            prior_state = manifest.get("supplements") if isinstance(manifest.get("supplements"), dict) else {}
            prior_inputs = prior_state.get("inputs") if isinstance(prior_state.get("inputs"), list) else []
            if "supplements" not in paper and prior_inputs:
                paper["supplements"] = prior_inputs
            manual, rejected = _manual_inputs(paper)
            pmcid = paper.get("pmcid")
            pmc_valid = isinstance(pmcid, str) and PMCID_RE.fullmatch(pmcid) is not None
            pmc_eligible = pmc_supplements_eligible(paper)
            pmc_license = paper.get("supplements_license") if pmc_eligible else None
            pmc_info = ({"eligible": True, "pmcid": pmcid, "license": pmc_license}
                        if pmc_eligible else {"eligible": False,
                            "reason": "PMC_OA_LICENSE_REQUIRED" if pmc_valid else "PMC_ID_REQUIRED"})
            prior_results = prior_state.get("results") if isinstance(prior_state.get("results"), list) else []
            inputs_to_store = manual
            results: list[dict[str, Any]] = list(rejected)
            failures = bool(rejected)
            pmc_complete = False

            if not pmc_eligible and not manual and not rejected:
                _save_manifest(pdf, manifest, status="not_checked", results=[], inputs=[],
                               pmc_eligibility=pmc_info, pmc_complete=False)
                return {"supplements_status": "not_checked", "supplements": [], "bundle_path": str(bundle_path)}
            if not pmc_eligible and not manual and rejected:
                _save_manifest(pdf, manifest, status="partial", results=results, inputs=[],
                               pmc_eligibility=pmc_info, pmc_complete=False)
                return {"supplements_status": "partial", "supplements": results,
                        "bundle_path": str(bundle_path)}

            supplement_dir = pdf.parent / "supplements"
            _regular_chain(supplement_dir)
            supplement_dir.mkdir(parents=True, exist_ok=True)
            _regular_chain(supplement_dir)
            if transport is None:
                from publishers.base import UrllibTransport
                transport = UrllibTransport()
            budget_dir = pdf.parent.parent
            if not _governor_policy_ok(governor):
                results.append({"status": "failed", "error": "SUPPLEMENT_RATE_POLICY_INVALID"})
                _save_manifest(pdf, manifest, status="partial", results=results, inputs=inputs_to_store,
                               pmc_eligibility=pmc_info, pmc_complete=False)
                return {"supplements_status": "partial", "supplements": results,
                        "bundle_path": str(bundle_path)}

            # The official endpoint returns one ZIP containing the available
            # OA supplementary files. A previous complete check is reusable.
            if pmc_eligible:
                prior_pmc_complete = prior_state.get("pmc_zip_checked") is True
                if prior_pmc_complete:
                    pmc_complete = True
                    results.extend(r for r in prior_results if isinstance(r, dict)
                                   and r.get("source") == "europepmc:supplementaryFiles"
                                   and r.get("status") in {"downloaded", "exists"}
                                   and not _is_pmc_article_figure_rendition(r.get("filename", ""))
                                   and _existing_record(verified, r.get("filename", ""),
                                       "europepmc:supplementaryFiles", pmc_license))
                    prior_failures = []
                    reclassified = []
                    archived_figures = []
                    for record in prior_results:
                        if (isinstance(record, dict) and record.get("source") == "europepmc:supplementaryFiles"
                                and record.get("status") in {"downloaded", "exists"}
                                and isinstance(record.get("filename"), str)
                                and _is_pmc_article_figure_rendition(record["filename"])):
                            excluded = _archive_pmc_figure_rendition(record, pdf=pdf,
                                manifest=manifest, verified=verified)
                            archived_figures.append(excluded)
                            prior_failures.append(excluded)
                        elif (isinstance(record, dict) and record.get("status") == "failed"
                                and record.get("error") == "SUPPLEMENT_FORMAT_UNSUPPORTED"
                                and isinstance(record.get("filename"), str)
                                and _is_pmc_article_figure_rendition(record["filename"])):
                            reclassified.append(record)
                            prior_failures.append({"status": "excluded", "filename": record["filename"],
                                "source": record.get("source", "europepmc:supplementaryFiles"),
                                "reason_code": "PMC_MAIN_ARTICLE_FIGURE_RENDITION",
                                "previous_error": record["error"]})
                        elif (isinstance(record, dict) and record.get("status") in {"failed", "excluded"}
                                and record.get("filename")):
                            prior_failures.append(record)
                    if reclassified or archived_figures:
                        history = prior_state.get("reconciliations")
                        if not isinstance(history, list):
                            history = []
                        if not any(isinstance(item, dict) and item.get("rule") == "pmc-figure-rendition-v1"
                                   for item in history):
                            if reclassified:
                                history.append({"rule": "pmc-figure-rendition-v1",
                                "updated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                                "reclassified_failures": reclassified})
                        if archived_figures and not any(isinstance(item, dict)
                                and item.get("rule") == "pmc-downloaded-figure-rendition-v1" for item in history):
                            history.append({"rule": "pmc-downloaded-figure-rendition-v1",
                                "updated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                                "reclassified_files": archived_figures})
                        prior_state["reconciliations"] = history
                        manifest["supplements"] = prior_state
                    results.extend(prior_failures)
                    failures = failures or any(r.get("status") == "failed" for r in prior_failures)
                else:
                    url = PMC_SUPPLEMENT_URL.format(pmcid=pmcid.upper())
                    temp_zip = None
                    try:
                        temp_zip, _ = _download_temp(url, temp_dir=supplement_dir, governor=governor,
                            transport=transport, budget_dir=budget_dir, limit=MAX_ARCHIVE_BYTES,
                            extension=".zip", archive=True)
                        pmc_results, pmc_failures, pmc_excluded = _extract_pmc_zip(temp_zip,
                            supplement_dir=supplement_dir, verified=verified,
                            license_value=pmc_license, budget_dir=budget_dir)
                        for result in pmc_results:
                            result["request_url"] = url
                        results.extend(pmc_results)
                        results.extend(pmc_failures)
                        results.extend(pmc_excluded)
                        # Inspection completed even when individual members are
                        # unsupported. Retain those outcomes without fetching
                        # the identical archive on every continuation.
                        pmc_complete = True
                        if pmc_failures:
                            failures = True
                    except _SupplementFailure as failure:
                        failures = True
                        results.append({"status": "failed", "source": "europepmc:supplementaryFiles",
                                        "error": failure.code})
                    finally:
                        if temp_zip is not None:
                            temp_zip.unlink(missing_ok=True)

            for index, record in enumerate(manual):
                try:
                    result = _download_manual(record, supplement_dir=supplement_dir,
                        budget_dir=budget_dir, governor=governor, transport=transport,
                        verified=verified, prior_results=prior_results, index=index)
                    results.append(result)
                    if (result.get("status") in {"downloaded", "exists"}
                            and _existing_record(verified, result.get("filename", ""),
                                result.get("source"), result.get("license")) is None):
                        verified.append({**result, "path": supplement_dir / record["filename"]})
                except _SupplementFailure as failure:
                    failures = True
                    results.append({"status": "failed", "filename": record["filename"],
                                    "source": record["source"], "error": failure.code})
            if failures:
                status = "partial"
            elif pmc_eligible or manual:
                status = "complete"
            else:
                status = "not_checked"
            bundle_path_value = _save_manifest(pdf, manifest, status=status, results=results,
                inputs=inputs_to_store, pmc_eligibility=pmc_info,
                pmc_complete=pmc_complete or (pmc_eligible and prior_state.get("pmc_zip_checked") is True))
            output = {"supplements_status": status, "supplements": results,
                      "bundle_path": bundle_path_value}
            if failures and not any(item.get("status") == "failed" for item in results):
                output["supplements_error"] = "SUPPLEMENT_ACQUISITION_PARTIAL"
            return output
    except _SupplementFailure as failure:
        return {**empty, "supplements_status": "partial", "supplements_error": failure.code}
    except BundleError as failure:
        code = str(failure) if re.fullmatch(r"[A-Z0-9_]{1,80}", str(failure)) else "BUNDLE_NEEDS_REVIEW"
        return {**empty, "supplements_status": "partial", "supplements_error": code}
    except (OSError, ValueError, TypeError):
        return {**empty, "supplements_status": "partial", "supplements_error": "BUNDLE_NEEDS_REVIEW"}
    except Exception:
        # Never leak transport or server exceptions, which may contain URLs.
        return {**empty, "supplements_status": "partial", "supplements_error": "SUPPLEMENT_ACQUISITION_FAILED"}


def _paper_from_bundle(bundle: Mapping[str, Any]) -> dict[str, Any]:
    paper = dict(bundle.get("paper", {}))
    state = bundle.get("supplements", {})
    if isinstance(state, dict):
        eligibility = state.get("pmc_eligibility")
        if isinstance(eligibility, dict) and eligibility.get("eligible") is True:
            paper["supplements_oa_eligible"] = True
            paper["supplements_license"] = eligibility.get("license")
        if isinstance(state.get("inputs"), list):
            paper["supplements"] = state["inputs"]
    return paper


def register_local_supplement(paper, pdf_path, local_path, record, expected_sha256):
    """Recover a separately acquired, hash-bound original without downloading.

    The caller must first verify the file's official mother-paper association
    and access terms. Local reuse requires the same confirmed direct-link record
    as acquisition, plus an independently verified SHA256. Originals are retained.
    """
    pdf, local = Path(pdf_path).absolute(), Path(local_path).absolute()
    _regular_chain(local)
    if not isinstance(expected_sha256, str) or not re.fullmatch("[0-9a-f]{64}", expected_sha256):
        raise BundleError("SUPPLEMENT_EXPECTED_HASH_REQUIRED")
    with process_lock(GLOBAL_PROCESS_LOCK):
        manifest = read_bundle(pdf.parent)
        if manifest is None:
            raise BundleError("BUNDLE_NOT_RECORDED")
        paper = _bound_paper(paper, manifest["paper"])
        inputs, rejected = _manual_inputs({**paper, "supplements": [record]})
        if rejected or len(inputs) != 1:
            raise BundleError("SUPPLEMENT_RECORD_REQUIRES_CONFIRMED_PUBLIC_LINK")
        record = inputs[0]
        identity = file_identity(local)
        limit = MAX_ARCHIVE_BYTES if Path(record["filename"]).suffix.lower() == ".zip" else MAX_SINGLE_FILE_BYTES
        if identity["sha256"] != expected_sha256 or identity["size_bytes"] > limit:
            raise BundleError("SUPPLEMENT_LOCAL_HASH_OR_SIZE_INVALID")
        mime = _validate_file(local, record["filename"], None)
        verified = verified_supplements(pdf)
        current = _existing_record(verified, record["filename"], record["source"], record["license"])
        registered = _existing_record(verified, record["filename"])
        if registered is not None and (current is None or current["sha256"] != expected_sha256):
            raise BundleError("SUPPLEMENT_FILENAME_COLLISION")
        if current is None and sum(r["size_bytes"] for r in verified) + identity["size_bytes"] > MAX_BUNDLE_BYTES:
            raise BundleError("SUPPLEMENT_BUNDLE_SIZE_LIMIT_EXCEEDED")
        destination = pdf.parent / "supplements" / record["filename"]
        _regular_chain(destination)
        destination.parent.mkdir(exist_ok=True)
        temporary = None
        try:
            if destination.exists():
                if file_identity(destination) != identity:
                    raise BundleError("SUPPLEMENT_FILENAME_COLLISION")
            else:
                fd, name = tempfile.mkstemp(prefix=".reuse-", suffix=".partial", dir=destination.parent)
                temporary = Path(name)
                with os.fdopen(fd, "wb") as target, local.open("rb") as source:
                    copied = 0
                    while block := source.read(CHUNK_SIZE):
                        copied += len(block)
                        if copied > limit:
                            raise BundleError("SUPPLEMENT_SIZE_LIMIT_EXCEEDED")
                        target.write(block)
                    target.flush()
                    os.fsync(target.fileno())
                if file_identity(temporary) != identity or file_identity(local) != identity:
                    raise BundleError("SUPPLEMENT_LOCAL_CHANGED")
                _publish_temp(temporary, destination)
                temporary = None
            result = {**record, **identity, "content_type": mime, "status": "exists",
                      "relative_path": "supplements/" + record["filename"], "request_url": record["url"],
                      "acquisition": "verified_local_reuse", "network_requests": 0}
            state = manifest.get("supplements", {})
            prior = state.get("results", [])
            results = [r for r in prior if r.get("filename") != record["filename"]] + [result]
            saved_paper = _paper_from_bundle(manifest)
            _merge_manifest_inputs(saved_paper, [record])
            valid, rejected = _manual_inputs(saved_paper)
            complete = not rejected and all(any(r.get("request_url") == i["url"]
                and r.get("filename") == i["filename"] and r.get("status") in {"downloaded", "exists"}
                for r in results) for i in valid)
            if pmc_supplements_eligible(saved_paper) and state.get("pmc_zip_checked") is not True:
                complete = False
            status = "complete" if complete else "partial"
            path = _save_manifest(pdf, manifest, status=status, results=results, inputs=valid,
                pmc_eligibility=state.get("pmc_eligibility"), pmc_complete=False)
            return {"supplements_status": status, "supplements": results, "bundle_path": path,
                    "network_requests": 0, "original_retained": True}
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _load_supplement_manifest(path: Path) -> list[dict[str, Any]]:
    _regular_chain(path.absolute())
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("SUPPLEMENT_MANIFEST_INVALID")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("SUPPLEMENT_MANIFEST_INVALID") from None
    if type(value) is not dict or set(value) != {"supplements"} or type(value["supplements"]) is not list:
        raise ValueError("SUPPLEMENT_MANIFEST_INVALID")
    if len(value["supplements"]) > MAX_INPUT_RECORDS:
        raise ValueError("SUPPLEMENT_RECORD_LIMIT_EXCEEDED")
    return value["supplements"]


def _merge_manifest_inputs(paper: dict[str, Any], extra: list[dict[str, Any]]) -> None:
    current = paper.get("supplements", [])
    if type(current) is not list:
        raise ValueError("SUPPLEMENT_RECORDS_INVALID")
    merged, seen = [], set()
    for record in [*current, *extra]:
        if not isinstance(record, dict):
            merged.append(record)
            continue
        key = (record.get("url"), record.get("filename"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(record)
    if len(merged) > MAX_INPUT_RECORDS:
        raise ValueError("SUPPLEMENT_RECORD_LIMIT_EXCEEDED")
    paper["supplements"] = merged


def main(argv=None, *, governor=None, transport=None) -> int:
    parser = argparse.ArgumentParser(description="Retry verified supplement acquisition for an existing paper bundle.")
    parser.add_argument("--pdf", required=True, type=Path, help="Already downloaded PDF inside its paper bundle")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--retry", action="store_true", help="Explicitly execute eligible supplement requests")
    mode.add_argument("--preview", action="store_true", help="Validate saved/new inputs without network or writes")
    mode.add_argument("--register-local", action="store_true", help="Register one confirmed hash-bound local original; no network")
    parser.add_argument("--reuse-local", type=Path, help="Original to retain and copy into supplements/")
    parser.add_argument("--expected-sha256", help="SHA256 independently verified against the selected original")
    parser.add_argument("--manifest", type=Path,
                        help="JSON file containing only {\"supplements\":[confirmed direct-link records]}")
    args = parser.parse_args(argv)
    try:
        pdf = args.pdf.absolute()
        _regular_chain(pdf)
        if not pdf.is_file():
            raise ValueError("MAIN_PDF_MISSING")
        manifest = read_bundle(pdf.parent)
        if manifest is None:
            raise ValueError("BUNDLE_NOT_RECORDED")
        verified_supplements(pdf)
        paper = _paper_from_bundle(manifest)
        if args.manifest is not None:
            _merge_manifest_inputs(paper, _load_supplement_manifest(args.manifest))
        paper = _bound_paper(paper, manifest.get("paper", {}))
        if args.register_local:
            records = _load_supplement_manifest(args.manifest) if args.manifest else []
            if len(records) != 1 or args.reuse_local is None or args.expected_sha256 is None:
                raise ValueError("LOCAL_REUSE_REQUIRES_ONE_CONFIRMED_RECORD_AND_HASH")
            result = register_local_supplement(paper, pdf, args.reuse_local, records[0], args.expected_sha256)
            print(json.dumps(result, ensure_ascii=True, indent=2))
            return 0 if result["supplements_status"] == "complete" else 2
        if args.reuse_local is not None or args.expected_sha256 is not None:
            raise ValueError("LOCAL_REUSE_REQUIRES_REGISTER_LOCAL")
        if args.preview:
            valid, rejected = _manual_inputs(paper)
            eligible = pmc_supplements_eligible(paper)
            preview = {"mode": "preview", "supplements_status": "not_checked",
                "bundle_path": str(pdf.parent / "bundle.json"), "pmc_eligible": eligible,
                "manual_records": [row["filename"] for row in valid],
                "rejected_records": rejected, "network_requests": 0, "written": False}
            print(json.dumps(preview, ensure_ascii=True, indent=2))
            return 0
        if governor is None:
            governor = RequestGovernor(ROOT / "download_state.json", interval_seconds=10,
                                       max_requests_per_minute=6)
        result = acquire_supplements(paper, pdf, governor=governor, transport=transport)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0 if result.get("supplements_status") in {"complete", "not_checked"} else 2
    except Exception:
        print(json.dumps({"supplements_status": "partial",
                          "supplements_error": "BUNDLE_NEEDS_REVIEW"}, ensure_ascii=True, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
