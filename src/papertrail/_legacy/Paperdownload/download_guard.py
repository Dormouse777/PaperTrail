#!/usr/bin/env python3
"""Persistent, conservative per-domain request and download-attempt guard.

Only hostnames and timing/error counters are saved; never URLs, cookies, or
credentials. A paused domain requires explicit human review before resume.
Every PaperDownloader network operation is wrapped by the installation-wide
cross-process lock; the guard state therefore remains serial for CLI and direct
Python callers.
"""

from __future__ import annotations

import json
import math
import os
import random
import stat
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit


STATE_VERSION = 1
_REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _is_owned_regular_temp(info, identity: tuple[int, int]) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    return (
        stat.S_ISREG(info.st_mode)
        and not (attributes & _REPARSE_POINT_ATTRIBUTE)
        and (info.st_dev, info.st_ino) == identity
    )


def safe_download_diagnostics(value: object) -> dict:
    """Retain failure facts without response bodies, URLs, or header values."""
    if type(value) is not dict:
        return {}
    result = {}
    code = value.get('http_status')
    if type(code) is int and 100 <= code <= 599:
        result['http_status'] = code
    kind = value.get('failure_kind')
    if type(kind) is str and kind in {'http_error', 'login_html', 'domain_paused'}:
        result['failure_kind'] = kind
    return result


class GuardError(RuntimeError):
    def __init__(self, status: str, domain: str, retry_at: float | None = None,
                 *, http_status: int | None = None, failure_kind: str | None = None):
        self.status = status
        self.domain = domain
        self.retry_at = retry_at
        self.diagnostics = safe_download_diagnostics({'http_status': http_status,
                                                      'failure_kind': failure_kind})
        suffix = f"; retry after {datetime.fromtimestamp(retry_at, timezone.utc).isoformat()}" if retry_at else ""
        super().__init__(f"{status} for {domain}{suffix}")


def domain_for(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only HTTP(S) URLs without embedded credentials are supported")
    hostname = parsed.hostname.lower()
    if hostname == "arxiv.org" or hostname.endswith(".arxiv.org"):
        return "arxiv.org"  # official limit spans legacy arXiv API hosts
    return hostname


def retry_after_seconds(value: str | None, now: float) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
    except ValueError:
        pass
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return max(0.0, date.timestamp() - now)
    except (TypeError, ValueError, OverflowError):
        return None


class RequestGovernor:
    def __init__(self, state_path: Path, *, interval_seconds: float = 5.0,
                 max_requests_per_minute: int = 12, max_downloads_per_hour: int = 20,
                 max_auto_wait_seconds: float = 15.0, jitter_seconds: float = 1.0,
                 clock=time.time, sleeper=time.sleep, jitter=random.random):
        if interval_seconds < 3 or max_requests_per_minute < 1 or max_downloads_per_hour < 1:
            raise ValueError("Unsafe or invalid governor limits")
        if max_auto_wait_seconds < 0 or not 0 <= jitter_seconds <= 10:
            raise ValueError("Invalid wait/jitter settings")
        self.path = Path(state_path)
        self.interval = interval_seconds
        self.per_minute = max_requests_per_minute
        self.downloads_per_hour = max_downloads_per_hour
        self.max_auto_wait = max_auto_wait_seconds
        self.jitter_seconds = jitter_seconds
        self.clock = clock
        self.sleeper = sleeper
        self.jitter = jitter
        if self.path.exists():
            try:
                self.state = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ValueError(f"Cannot read request guard state: {self.path}") from exc
            if (not isinstance(self.state, dict) or self.state.get("version") != STATE_VERSION
                    or not isinstance(self.state.get("domains"), dict)):
                raise ValueError(f"Unsupported request guard state: {self.path}")
        else:
            self.state = {"version": STATE_VERSION, "domains": {}}

    def _entry(self, domain: str) -> dict:
        return self.state["domains"].setdefault(domain, {
            "request_times": [], "download_attempt_times": [], "cooldown_until": 0.0,
            "paused_reason": None, "consecutive_errors": 0,
        })

    def _save(self) -> None:
        temporary = None
        descriptor = None
        identity = None
        replaced = False
        write_failed = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f"{self.path.stem}.{uuid.uuid4().hex}.tmp")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(temporary, flags, 0o600)
            created = os.fstat(descriptor)
            identity = (created.st_dev, created.st_ino)
            try:
                stream = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
            except BaseException:
                try:
                    os.close(descriptor)
                except Exception:
                    pass
                descriptor = None
                raise
            descriptor = None  # stream owns the descriptor now

            write_error = None
            try:
                json.dump(self.state, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException as exc:
                write_error = exc
                raise
            finally:
                try:
                    stream.close()
                except Exception:
                    if write_error is None:
                        raise

            current = temporary.lstat()
            if not _is_owned_regular_temp(current, identity):
                raise OSError("GUARD_STATE_WRITE_FAILED")
            os.replace(temporary, self.path)
            replaced = True
        except Exception:
            write_failed = True
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except Exception:
                    write_failed = True
            if temporary is not None and not replaced and identity is not None:
                try:
                    current = temporary.lstat()
                except FileNotFoundError:
                    pass
                except Exception:
                    write_failed = True
                else:
                    if _is_owned_regular_temp(current, identity):
                        try:
                            temporary.unlink()
                        except FileNotFoundError:
                            pass
                        except Exception:
                            # Keep the write failure primary; never remove an unverified path.
                            write_failed = True
        if write_failed:
            raise OSError("GUARD_STATE_WRITE_FAILED") from None

    def acquire(self, url: str, *, download_attempt: bool = False) -> None:
        """Record an outgoing attempt before network I/O, or defer it safely."""
        domain = domain_for(url)
        entry = self._entry(domain)
        if entry["paused_reason"]:
            raise GuardError(entry["paused_reason"], domain)
        while True:
            now = self.clock()
            requests = [t for t in entry["request_times"] if t > now - 60]
            downloads = [t for t in entry["download_attempt_times"] if t > now - 3600]
            entry["request_times"], entry["download_attempt_times"] = requests, downloads
            allowed_at = max(now, entry["cooldown_until"])
            if requests:
                allowed_at = max(allowed_at, requests[-1] + self.interval + self.jitter_seconds * self.jitter())
            if len(requests) >= self.per_minute:
                allowed_at = max(allowed_at, requests[0] + 60)
            if download_attempt and len(downloads) >= self.downloads_per_hour:
                allowed_at = max(allowed_at, downloads[0] + 3600)
            delay = allowed_at - now
            if delay <= 0:
                requests.append(now)
                if download_attempt:
                    downloads.append(now)
                self._save()
                return
            self._save()  # preserve pruning/cooldown before any wait
            if delay > self.max_auto_wait:
                raise GuardError("RATE_LIMITED", domain, allowed_at)
            self.sleeper(delay)

    def record_http_error(self, url: str, status_code: int, retry_after: str | None = None,
                          *, authenticated: bool = False) -> GuardError | None:
        domain = domain_for(url)
        entry = self._entry(domain)
        now = self.clock()
        if status_code in {401, 403}:
            entry["paused_reason"] = "AUTH_EXPIRED" if authenticated else "ACCESS_DENIED"
            self._save()
            return GuardError(entry["paused_reason"], domain,
                              http_status=status_code, failure_kind='http_error')
        if status_code not in {406, 429, 500, 502, 503, 504}:
            return None
        entry["consecutive_errors"] += 1
        retry_seconds = retry_after_seconds(retry_after, now)
        backoff = min(3600, 5 * 2 ** min(entry["consecutive_errors"] - 1, 8))
        entry["cooldown_until"] = max(entry["cooldown_until"], now + max(backoff, retry_seconds or 0))
        if entry["consecutive_errors"] >= 3:
            entry["paused_reason"] = "DOMAIN_PAUSED"
        self._save()
        if entry["paused_reason"]:
            return GuardError("DOMAIN_PAUSED", domain,
                              http_status=status_code, failure_kind='http_error')
        return GuardError("RETRY_WAIT", domain, entry["cooldown_until"],
                          http_status=status_code, failure_kind='http_error')

    def pause_domain(self, url: str, reason: str) -> GuardError:
        """Persist a manual-review pause discovered from a successful HTTP response."""
        if reason not in {"AUTH_EXPIRED", "ACCESS_DENIED"}:
            raise ValueError("Only authentication/access failures may pause a domain here")
        domain = domain_for(url)
        entry = self._entry(domain)
        entry["paused_reason"] = reason
        self._save()
        return GuardError(reason, domain)

    def record_success(self, url: str) -> None:
        entry = self._entry(domain_for(url))
        if entry["consecutive_errors"]:
            entry["consecutive_errors"] = 0
            self._save()

    def resume_domain(self, domain: str) -> None:
        """Manual operation after access/authorization has been reviewed."""
        if not domain or "/" in domain or ":" in domain:
            raise ValueError("Expected a hostname, not a URL")
        entry = self._entry(domain.lower())
        entry["paused_reason"] = None
        entry["consecutive_errors"] = 0
        # Do not clear timing or Retry-After limits when resuming.
        self._save()
