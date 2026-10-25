"""Fail-closed transport for Zotero's loopback Local API v3.

This module deliberately implements transport and authorization only. It does
not create or file bibliographic items. Remembered grants use Windows Credential
Manager; keys never enter project files, command output, or logs.
"""

from __future__ import annotations

import json
import math
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any


API_ORIGIN = "http://127.0.0.1:23119"
API_BASE = f"{API_ORIGIN}/api"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
DEFAULT_TIMEOUT = 15.0
MAX_TIMEOUT = 60.0
DEFAULT_APP_NAME = "Trae Research Paper Filing"

_SAFE_CODES = frozenset({
    "API_ERROR",
    "API_UNAVAILABLE",
    "AUTH_DENIED",
    "AUTH_REQUIRED",
    "AUTH_STORE_UNAVAILABLE",
    "AUTH_STORE_INVALID",
    "INVALID_JSON",
    "INVALID_REQUEST",
    "INVALID_RESPONSE",
    "INVALID_UPLOAD_URL",
    "PRECONDITION_FAILED",
    "RATE_LIMITED",
    "REDIRECT_REJECTED",
    "RESPONSE_TOO_LARGE",
    "SERVER_ID_MISMATCH",
    "TIMEOUT",
    "UNSUPPORTED_API_VERSION",
    "UPLOAD_FAILED",
})
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_UPLOAD_KEY = re.compile(r"[A-Za-z0-9_-]+\Z")
_MIME_TYPE = re.compile(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+\Z")
_BLOCKED_QUERY_NAMES = frozenset({
    "key", "apikey", "zoteroapikey", "auth", "authorization", "token",
    "accesstoken", "password", "secret", "session",
})
_BLOCKED_HEADERS = frozenset({
    "authorization", "cookie", "proxy-authorization", "zotero-api-key",
    "host",
})


class ZoteroApiFailure(RuntimeError):
    """Sanitized transport failure containing only a fixed code and status."""

    def __init__(self, code: str, status: int | None = None):
        self.code = code if code in _SAFE_CODES else "API_ERROR"
        self.status = status if isinstance(status, int) and 100 <= status <= 599 else None
        super().__init__(self.code)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _failure(code: str, status: int | None = None) -> ZoteroApiFailure:
    return ZoteroApiFailure(code, status)


def _header_value(headers: Mapping[str, Any] | Any, name: str) -> str | None:
    if headers is None:
        return None
    try:
        pairs = headers.items()
    except Exception:
        return None
    wanted = name.casefold()
    for key, value in pairs:
        if isinstance(key, str) and key.casefold() == wanted:
            return value if isinstance(value, str) else str(value)
    return None


def _copy_response_headers(headers: Any) -> dict[str, str]:
    if headers is None:
        return {}
    try:
        pairs = headers.items()
        result = {}
        for name, value in pairs:
            result[str(name)] = str(value)
        return result
    except Exception:
        return {}


def _strict_json(raw: bytes) -> Any:
    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON property")
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError("non-finite JSON number")

    failed = False
    value = None
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except Exception:
        failed = True
    if failed:
        raise _failure("INVALID_RESPONSE")
    return value


def _parse_json_object_or_list(raw: bytes) -> dict | list | None:
    if not raw:
        return None
    value = _strict_json(raw)
    if not isinstance(value, (dict, list)):
        raise _failure("INVALID_RESPONSE")
    return value


def _normalize_api_path(path: str) -> tuple[str, str]:
    """Return a canonical API route and encoded query, rejecting authority data."""
    if not isinstance(path, str) or not path or "\\" in path or "\x00" in path:
        raise _failure("INVALID_REQUEST")
    parse_failed = False
    parsed = None
    try:
        parsed = urllib.parse.urlsplit(path)
    except Exception:
        parse_failed = True
    if parse_failed:
        raise _failure("INVALID_REQUEST")
    if parsed.scheme or parsed.netloc or path.startswith("//") or parsed.fragment:
        raise _failure("INVALID_REQUEST")

    raw_route = parsed.path
    if raw_route == "/api":
        raw_route = "/"
    elif raw_route.startswith("/api/"):
        raw_route = raw_route[4:]
    elif raw_route == "api":
        raw_route = "/"
    elif raw_route.startswith("api/"):
        raw_route = "/" + raw_route[4:]
    elif not raw_route.startswith("/"):
        raw_route = "/" + raw_route

    if re.search(r"%(?:2f|5c|25)", raw_route, re.IGNORECASE):
        raise _failure("INVALID_REQUEST")
    decode_failed = False
    route = ""
    try:
        route = urllib.parse.unquote(raw_route, errors="strict")
    except Exception:
        decode_failed = True
    if decode_failed:
        raise _failure("INVALID_REQUEST")
    if "\\" in route or "\x00" in route:
        raise _failure("INVALID_REQUEST")
    if any(segment in {".", ".."} for segment in route.split("/")):
        raise _failure("INVALID_REQUEST")
    if route != "/" and not (
        route == "/local/authorize"
        or route == "/users/0"
        or route.startswith("/users/0/")
    ):
        raise _failure("INVALID_REQUEST")

    query_failed = False
    query_items = []
    try:
        query_items = urllib.parse.parse_qsl(
            parsed.query, keep_blank_values=True, strict_parsing=False,
            encoding="utf-8", errors="strict",
        )
        query = urllib.parse.urlencode(query_items, doseq=True)
    except Exception:
        query_failed = True
        query = ""
    if query_failed:
        raise _failure("INVALID_REQUEST")
    for name, _value in query_items:
        normalized = re.sub(r"[-_]", "", name).casefold()
        if normalized in _BLOCKED_QUERY_NAMES:
            raise _failure("INVALID_REQUEST")
    return route, query


def _safe_request_headers(headers: Mapping[str, Any] | None) -> dict[str, str]:
    if headers is None:
        return {}
    if not isinstance(headers, Mapping):
        raise _failure("INVALID_REQUEST")
    result: dict[str, str] = {}
    seen: set[str] = set()
    for name, value in headers.items():
        if (not isinstance(name, str) or not _HEADER_NAME.fullmatch(name)
                or not isinstance(value, str) or "\r" in value or "\n" in value):
            raise _failure("INVALID_REQUEST")
        normalized = name.casefold()
        if normalized in _BLOCKED_HEADERS or normalized in seen:
            raise _failure("INVALID_REQUEST")
        if normalized in {"server-id", "zotero-server-id", "zotero-api-version"}:
            raise _failure("INVALID_REQUEST")
        seen.add(normalized)
        result[name] = value
    return result


def _status_failure(status: int) -> ZoteroApiFailure:
    code = {
        401: "AUTH_DENIED",
        403: "AUTH_DENIED",
        412: "PRECONDITION_FAILED",
        429: "RATE_LIMITED",
    }.get(status, "API_ERROR")
    return _failure(code, status)


class ZoteroLocalClient:
    """Client for the Zotero 10 loopback Local API and its upload flow."""

    def __init__(self, opener=None, timeout: float = DEFAULT_TIMEOUT,
                 app_name: str = DEFAULT_APP_NAME, credential_store=None):
        timeout_failed = False
        numeric_timeout = 0.0
        try:
            numeric_timeout = float(timeout)
        except Exception:
            timeout_failed = True
        if timeout_failed:
            raise ValueError("Invalid timeout")
        if not math.isfinite(numeric_timeout) or numeric_timeout <= 0:
            raise ValueError("Invalid timeout")
        if not isinstance(app_name, str) or not app_name.strip() or len(app_name) > 200:
            raise ValueError("Invalid application name")
        if any(ord(char) < 32 or ord(char) == 127 for char in app_name):
            raise ValueError("Invalid application name")

        self.timeout = min(numeric_timeout, MAX_TIMEOUT)
        self.app_name = app_name
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirectHandler()
        )
        self.server_id: str | None = None
        self.api_version: str | None = None
        self.remembered: bool = False
        self._api_key: str | None = None
        # Fake transports stay isolated from the user's credential store.
        if credential_store is None and opener is None and os.name == "nt":
            from zotero_credentials import WindowsCredentialStore
            credential_store = WindowsCredentialStore()
        self._credential_store = credential_store

    def _stored_grant(self, operation, *args):
        if self._credential_store is None:
            return None
        from zotero_credentials import CredentialStoreError
        try:
            return getattr(self._credential_store, operation)(
                self.server_id, self.app_name, *args)
        except CredentialStoreError as exc:
            raise _failure(str(exc)) from None

    def _send_once(self, url: str, method: str, body: bytes | None,
                   headers: Mapping[str, str], *, timeout: float | None = None
                   ) -> tuple[int, bytes, dict[str, str]]:
        status: int | None = None
        response_body = b""
        response_headers: dict[str, str] = {}
        failure_code: str | None = None
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            request_timeout = self.timeout if timeout is None else min(timeout, MAX_TIMEOUT)
            with self.opener.open(request, timeout=request_timeout) as response:
                status = int(response.getcode())
                response_headers = _copy_response_headers(getattr(response, "headers", None))
                final_url = response.geturl() if hasattr(response, "geturl") else url
                if final_url is not None and final_url != url:
                    failure_code = "REDIRECT_REJECTED"
                else:
                    response_body = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status = exc.code if isinstance(exc.code, int) else None
            response_headers = _copy_response_headers(exc.headers)
            try:
                response_body = exc.read(MAX_RESPONSE_BYTES + 1)
            except Exception:
                failure_code = "API_ERROR"
        except (TimeoutError, socket.timeout):
            failure_code = "TIMEOUT"
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            failure_code = "TIMEOUT" if isinstance(reason, (TimeoutError, socket.timeout)) else "API_UNAVAILABLE"
        except Exception:
            failure_code = "API_UNAVAILABLE"

        if failure_code is not None:
            raise _failure(failure_code, status)
        if status is None:
            raise _failure("API_ERROR")
        if 300 <= status <= 399:
            raise _failure("REDIRECT_REJECTED", status)
        if len(response_body) > MAX_RESPONSE_BYTES:
            raise _failure("RESPONSE_TOO_LARGE", status)
        return status, response_body, response_headers

    def _check_bound_response(self, response_headers: Mapping[str, Any], status: int) -> None:
        # Preserve status-specific handling for errors such as 401/403/412/429.
        if not 200 <= status < 300:
            return
        response_server_id = _header_value(response_headers, "Zotero-Server-ID")
        if response_server_id is None:
            self._clear_key()
            raise _failure("INVALID_RESPONSE", status)
        if response_server_id != self.server_id:
            self._clear_key()
            raise _failure("SERVER_ID_MISMATCH", status)
        response_version = (_header_value(response_headers, "Zotero-API-Version")
                            or _header_value(response_headers, "API-Version"))
        if response_version is not None and response_version != self.api_version:
            self._clear_key()
            raise _failure("UNSUPPORTED_API_VERSION", status)

    def _clear_key(self) -> None:
        self._api_key = None
        self.remembered = False

    def discover(self) -> dict[str, Any]:
        """Discover and retain the local server binding; never expose its ID."""
        status, _body, response_headers = self._send_once(
            f"{API_BASE}/", "GET", None, {"Accept": "application/json"}
        )
        if not 200 <= status < 300:
            raise _status_failure(status)
        server_id = _header_value(response_headers, "Zotero-Server-ID")
        api_version = (_header_value(response_headers, "Zotero-API-Version")
                       or _header_value(response_headers, "API-Version"))
        if (not server_id or len(server_id) > 1024
                or any(ord(char) < 33 or ord(char) > 126 for char in server_id)):
            self._clear_key()
            raise _failure("INVALID_RESPONSE", status)
        if api_version != "3":
            self._clear_key()
            raise _failure("UNSUPPORTED_API_VERSION", status)
        if self.server_id is not None and self.server_id != server_id:
            self._clear_key()
        self.server_id = server_id
        self.api_version = api_version
        return {"available": True, "api_version": api_version}

    def _ensure_discovered(self) -> None:
        if self.server_id is None or self.api_version is None:
            self.discover()

    def _bound_headers(self, extra: Mapping[str, str], *, authenticated: bool) -> dict[str, str]:
        self._ensure_discovered()
        headers = dict(extra)
        headers["Zotero-Server-ID"] = self.server_id or ""
        headers["Zotero-API-Version"] = self.api_version or "3"
        if authenticated:
            if not self._api_key:
                raise _failure("AUTH_REQUIRED")
            headers["Zotero-API-Key"] = self._api_key
        return headers

    def request(self, method: str, path: str, *, json_body=None, form=None,
                data=None, headers=None, authenticated: bool = False
                ) -> tuple[int, bytes, dict[str, str]]:
        """Make one bounded request to the fixed loopback API origin."""
        if not isinstance(method, str):
            raise _failure("INVALID_REQUEST")
        method = method.upper()
        if method not in _WRITE_METHODS | {"GET"} or not isinstance(authenticated, bool):
            raise _failure("INVALID_REQUEST")
        route, query = _normalize_api_path(path)
        if route == "/local/authorize" or route.startswith("/local/uploads/"):
            raise _failure("INVALID_REQUEST")
        if route == "/" and method != "GET":
            raise _failure("INVALID_REQUEST")
        if route != "/" and not route.startswith("/users/0"):
            raise _failure("INVALID_REQUEST")
        if method in _WRITE_METHODS and not authenticated:
            raise _failure("AUTH_REQUIRED")
        if authenticated and method not in _WRITE_METHODS:
            raise _failure("INVALID_REQUEST")

        supplied_headers = _safe_request_headers(headers)
        body_modes = sum(value is not None for value in (json_body, form, data))
        if body_modes > 1:
            raise _failure("INVALID_REQUEST")
        body: bytes | None = None
        if json_body is not None:
            json_failed = False
            try:
                body = json.dumps(
                    json_body, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                ).encode("utf-8")
            except Exception:
                json_failed = True
            if json_failed:
                raise _failure("INVALID_JSON")
        elif form is not None:
            body = self._encode_form(form)
        elif data is not None:
            if not isinstance(data, (bytes, bytearray, memoryview)):
                raise _failure("INVALID_REQUEST")
            body = bytes(data)

        url = f"{API_BASE}{route}" + (f"?{query}" if query else "")
        request_headers = self._bound_headers(supplied_headers, authenticated=authenticated)
        request_headers.setdefault("Accept", "application/json")
        try:
            status, response_body, response_headers = self._send_once(
                url, method, body, request_headers
            )
            if status == 401:
                try:
                    self._stored_grant("delete")
                finally:
                    self._clear_key()
            if route != "/":
                self._check_bound_response(response_headers, status)
            return status, response_body, response_headers
        finally:
            # AllowOnce keys are single-use after any authenticated attempt.
            if authenticated and not self.remembered:
                self._clear_key()

    @staticmethod
    def _encode_form(fields: Any) -> bytes:
        if not isinstance(fields, Mapping):
            raise _failure("INVALID_REQUEST")
        for key, value in fields.items():
            if not isinstance(key, str):
                raise _failure("INVALID_REQUEST")
            values = value if isinstance(value, (list, tuple)) else (value,)
            if any(not isinstance(item, (str, int, bool)) for item in values):
                raise _failure("INVALID_REQUEST")
        encode_failed = False
        encoded = b""
        try:
            # Local file registration preserved '+' literally in a real
            # filename. Percent-encoded spaces are valid form encoding too
            # and avoid that ambiguity without renaming existing attachments.
            encoded = urllib.parse.urlencode(fields, doseq=True,
                                             quote_via=urllib.parse.quote).encode("ascii")
        except Exception:
            encode_failed = True
        if encode_failed:
            raise _failure("INVALID_REQUEST")
        return encoded

    @staticmethod
    def _expect_success(status: int) -> None:
        if not 200 <= status < 300:
            raise _status_failure(status)

    def get_json(self, path: str) -> dict | list:
        status, body, _headers = self.request("GET", path)
        self._expect_success(status)
        value = _parse_json_object_or_list(body)
        if value is None:
            raise _failure("INVALID_RESPONSE", status)
        return value

    def authorize(self) -> None:
        """Reuse an instance-bound grant before requesting interactive authorization."""
        if self.remembered and self._api_key and self.server_id and self.api_version == "3":
            return None
        # A fresh discovery binds a new explicit authorization attempt to the
        # currently running Zotero instance after any prior key was consumed.
        self.discover()
        cached = self._stored_grant("load")
        if cached is not None:
            if not isinstance(cached, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32}", cached):
                raise _failure("AUTH_STORE_INVALID")
            self._api_key, self.remembered = cached, True
            return None
        body = json.dumps(
            {"appName": self.app_name}, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "Zotero-Server-ID": self.server_id or "",
            "Zotero-API-Version": self.api_version or "3",
        }
        status, response_body, response_headers = self._send_once(
            f"{API_BASE}/local/authorize", "POST", body, headers,
            timeout=MAX_TIMEOUT,
        )
        self._check_bound_response(response_headers, status)
        if status in {401, 403}:
            self._clear_key()
            raise _failure("AUTH_DENIED", status)
        self._expect_success(status)
        value = _strict_json(response_body)
        if (not isinstance(value, dict)
                or set(value) - {"key", "remember"}
                or not isinstance(value.get("key"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{32}", value["key"])):
            self._clear_key()
            raise _failure("INVALID_RESPONSE", status)
        remember = value.get("remember", False)
        if not isinstance(remember, bool):
            self._clear_key()
            raise _failure("INVALID_RESPONSE", status)
        self._api_key = value["key"]
        self.remembered = remember
        if remember:
            try:
                self._stored_grant("save", self._api_key)
            except ZoteroApiFailure:
                self._clear_key()
                raise
        return None

    def write_json(self, method: str, path: str, value,
                   headers=None) -> dict | list | None:
        if not isinstance(method, str) or method.upper() not in _WRITE_METHODS:
            raise _failure("INVALID_REQUEST")
        supplied = _safe_request_headers(headers)
        supplied["Content-Type"] = "application/json; charset=utf-8"
        supplied.setdefault("Accept", "application/json")
        status, body, _response_headers = self.request(
            method, path, json_body=value, headers=supplied, authenticated=True
        )
        self._expect_success(status)
        return _parse_json_object_or_list(body)

    def write_form(self, path: str, fields,
                   headers=None) -> dict | None:
        supplied = _safe_request_headers(headers)
        supplied["Content-Type"] = "application/x-www-form-urlencoded; charset=utf-8"
        supplied.setdefault("Accept", "application/json")
        status, body, _response_headers = self.request(
            "POST", path, form=fields, headers=supplied, authenticated=True
        )
        self._expect_success(status)
        value = _parse_json_object_or_list(body)
        if value is not None and not isinstance(value, dict):
            raise _failure("INVALID_RESPONSE", status)
        return value

    @staticmethod
    def _validate_upload_url(url: str) -> str:
        if not isinstance(url, str):
            raise _failure("INVALID_UPLOAD_URL")
        parse_failed = False
        parsed = None
        port = None
        try:
            parsed = urllib.parse.urlsplit(url)
            port = parsed.port
        except Exception:
            parse_failed = True
        if parse_failed:
            raise _failure("INVALID_UPLOAD_URL")
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                or port != 23119 or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment):
            raise _failure("INVALID_UPLOAD_URL")
        prefix = "/api/local/uploads/"
        if not parsed.path.startswith(prefix):
            raise _failure("INVALID_UPLOAD_URL")
        key = parsed.path[len(prefix):]
        if not key or "/" in key or not _UPLOAD_KEY.fullmatch(key):
            raise _failure("INVALID_UPLOAD_URL")
        canonical = f"{API_ORIGIN}{prefix}{key}"
        if url != canonical:
            raise _failure("INVALID_UPLOAD_URL")
        return canonical

    def upload_file(self, url: str, data: bytes, content_type: str) -> None:
        """Upload bytes to the validated local temporary-upload URL without credentials."""
        target = self._validate_upload_url(url)
        self._ensure_discovered()
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise _failure("INVALID_REQUEST")
        if not isinstance(content_type, str) or not _MIME_TYPE.fullmatch(content_type):
            raise _failure("INVALID_REQUEST")
        status, _body, response_headers = self._send_once(
            target, "POST", bytes(data),
            {"Content-Type": content_type, "Accept": "application/json"},
            timeout=MAX_TIMEOUT,
        )
        if status == 401:
            self._clear_key()
        if status != 201:
            if status in {401, 403, 412, 429}:
                raise _status_failure(status)
            raise _failure("UPLOAD_FAILED", status)
        upload_server_id = _header_value(response_headers, "Zotero-Server-ID")
        if upload_server_id is None:
            self._clear_key()
            raise _failure("INVALID_RESPONSE", status)
        if upload_server_id != self.server_id:
            self._clear_key()
            raise _failure("SERVER_ID_MISMATCH", status)
        return None
