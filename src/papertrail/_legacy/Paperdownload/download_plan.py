"""Validate an untrusted, LLM-proposed literature search plan.

The plan can name only supported public search sources and bounded queries.
It cannot carry URLs, credentials, filesystem paths, or network settings.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


MAX_PLAN_BYTES = 32 * 1024
MAX_TASKS = 10
MAX_TOTAL_CANDIDATES = 25


class PlanError(ValueError):
    pass


_FILESYSTEM_PATH = re.compile(
    r"(?:^|\s)(?:[A-Za-z]:[\\/]|\\\\|~[/\\]|\.\.[/\\]|/(?:etc|home|root|tmp|var|Users?)(?:[/\\]|$))",
    re.IGNORECASE,
)
_SENSITIVE_QUERY_TEXT = re.compile(
    r"(?:"
    r"\b(?:api[ _-]?key|access[ _-]?token|bearer[ _-]?token|password|passwd|cookie|captcha|webvpn|carsi)\b"
    r"|\b(?:log[ -]?in|sign[ -]?in|institutional[ -]?access)\b"
    r"|\b(?:http[_-]?proxy|https[_-]?proxy|proxy[ _-]?server|user[ _-]?agent|download[ _-]?dir|output[ _-]?dir|save[ _-]?path)\b"
    r"|\bbypass\b.{0,40}\b(?:paywall|captcha|authentication|access[ -]?control)\b"
    r"|(?:登录|密码|口令|验证码|访问令牌|会话凭据|代理服务器|用户代理|机构认证|下载目录|保存路径)"
    r"|绕过.{0,20}(?:付费墙|验证码|身份认证|访问限制)"
    r")",
    re.IGNORECASE,
)


def _validate_query_boundary(query: str) -> None:
    if "://" in query or "@" in query or query.casefold().startswith("www."):
        raise PlanError("Queries must not contain URLs or credential-like text")
    if _FILESYSTEM_PATH.search(query):
        raise PlanError("Queries must not contain filesystem paths")
    if _SENSITIVE_QUERY_TEXT.search(query):
        raise PlanError("Queries must not contain credentials, login, network, or access-bypass settings")


def validate_plan(payload: object) -> list[dict]:
    if not isinstance(payload, dict) or set(payload) != {"version", "tasks"}:
        raise PlanError("Plan must contain only version and tasks")
    if type(payload["version"]) is not int or payload["version"] != 1:
        raise PlanError("Unsupported plan version")
    tasks = payload["tasks"]
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_TASKS:
        raise PlanError("Plan must have 1–10 tasks")
    normalized = []
    seen = set()
    total = 0
    for task in tasks:
        if not isinstance(task, dict) or set(task) - {"query", "source", "max_results", "sort", "open_access_only"}:
            raise PlanError("Task has unsupported fields")
        query = task.get("query")
        if not isinstance(query, str) or not 3 <= len(query.strip()) <= 300 or any(ord(ch) < 32 or ord(ch) == 127 for ch in query):
            raise PlanError("Each query must be 3–300 printable characters")
        query = query.strip()
        _validate_query_boundary(query)
        source = task.get("source")
        if source not in ("pubmed", "arxiv"):
            raise PlanError("Each task needs source pubmed or arxiv")
        maximum = task.get("max_results", 1)
        if type(maximum) is not int or not 1 <= maximum <= 10:
            raise PlanError("max_results must be an integer from 1 to 10")
        sort = task.get("sort", "date")
        if sort not in ("date", "relevance"):
            raise PlanError("sort must be date or relevance")
        oa_only = task.get("open_access_only", source == "pubmed")
        if type(oa_only) is not bool:
            raise PlanError("open_access_only must be boolean")
        if source == "arxiv" and oa_only:
            raise PlanError("open_access_only is a PubMed-only filter")
        identity = (source, query.casefold())
        if identity in seen:
            raise PlanError("Repeated source and query in one plan")
        seen.add(identity)
        total += maximum
        normalized.append({"query": query, "source": source, "max_results": maximum,
                           "sort": sort, "open_access_only": oa_only})
    if total > MAX_TOTAL_CANDIDATES:
        raise PlanError("Plan exceeds the 25-paper candidate cap")
    return normalized


def load_plan(path: Path) -> list[dict]:
    path = Path(path)
    if path.stat().st_size > MAX_PLAN_BYTES:
        raise PlanError("Plan exceeds 32 KiB")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise PlanError("Plan must be UTF-8 JSON") from exc
    return validate_plan(payload)
