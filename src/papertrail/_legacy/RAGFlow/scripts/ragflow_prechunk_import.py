#!/usr/bin/env python3
"""Import Structure-aware Chunker JSONL without invoking RAGFlow's parser.

Dry-run is the default. --execute requires a dataset ID and RAGFLOW_API_KEY.
The state file contains the full source chunk records for citation restoration,
but never the API key. This client targets the locally pinned RAGFlow v0.27.2 API.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


DEFAULT_BASE = "http://127.0.0.1:80/api/v1"
DEFAULT_STATE_DIR = Path(__file__).resolve().parents[1] / "artifacts" / "import-state"
IMPORTER_MARKER = "trae-mineru-prechunk-v1"
DOCUMENT_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


class ImportError_(RuntimeError):
    pass


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def text_digest(value: str) -> str:
    return digest(value.encode("utf-8"))


def load_bundle(path: Path) -> dict:
    raw = path.read_bytes()
    try:
        lines = raw.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError as exc:
        raise ImportError_(f"Chunk JSONL is not UTF-8: {path}") from exc
    rows: list[dict] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ImportError_(f"Invalid JSON on line {line_number}: {exc.msg}") from exc
        if not isinstance(row, dict):
            raise ImportError_(f"Line {line_number} must be an object")
        for field in ("chunk_id", "document_id", "title", "raw_text", "embedding_text"):
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise ImportError_(f"Line {line_number} has no non-empty {field}")
        if not isinstance(row.get("mineru_block_ids"), list) or not isinstance(row.get("locations"), list):
            raise ImportError_(f"Line {line_number} lacks traceable block/position arrays")
        if not isinstance(row.get("provenance"), dict):
            raise ImportError_(f"Line {line_number} lacks provenance")
        rows.append(row)
    if not rows:
        raise ImportError_("Chunk JSONL has no rows")
    source_id = rows[0]["document_id"]
    if not DOCUMENT_ID_PATTERN.fullmatch(source_id):
        raise ImportError_("document_id must be 1-64 letters, numbers, underscores or hyphens")
    title = rows[0]["title"]
    provenance = rows[0]["provenance"]
    if provenance.get("resource_type") == "jats_xml":
        try:
            from jats_import_contract import validate_jats_rows
            validate_jats_rows(rows)
        except Exception:
            raise ImportError_("JATS chunks do not match the verified original XML and sidecar") from None
        stable_provenance = ("resource_type", "source_xml_sha256", "source_xml", "source_sidecar",
                             "source_parser", "doi", "permission_evidence", "jats_product_evidence")
    elif provenance.get("resource_type") == "docx_ooxml":
        try:
            from docx_methods_source import validate_docx_rows
            validate_docx_rows(rows)
        except (ValueError, KeyError, TypeError, OSError, RuntimeError):
            raise ImportError_("DOCX chunks do not match the reviewed original and selection") from None
        stable_provenance = tuple(provenance)
    else:
        if provenance.get("resource_type") not in (None, "pdf"):
            raise ImportError_("Unsupported source resource type")
        source_hash = provenance.get("source_pdf_sha256")
        if not isinstance(source_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", source_hash):
            raise ImportError_("Paper provenance lacks a valid source_pdf_sha256")
        stable_provenance = ("source_pdf_sha256", "zotero_parent_key", "zotero_pdf_attachment_key")
    seen_ids: set[str] = set()
    seen_contents: set[str] = set()
    for row in rows:
        if row["document_id"] != source_id or row["title"] != title:
            raise ImportError_("All rows in one JSONL must identify the same paper and title")
        if any(row["provenance"].get(k) != provenance.get(k) for k in stable_provenance):
            raise ImportError_("Paper provenance differs between rows")
        if row["chunk_id"] in seen_ids:
            raise ImportError_(f"Repeated project chunk_id: {row['chunk_id']}")
        if row["embedding_text"] in seen_contents:
            raise ImportError_("Repeated embedding_text within a paper would collide in RAGFlow")
        seen_ids.add(row["chunk_id"])
        seen_contents.add(row["embedding_text"])
    return {
        "rows": rows,
        "by_content": {row["embedding_text"]: row for row in rows},
        "document_id": source_id,
        "document_name": f"trae-prechunk-{source_id}.md",
        "title": title,
        "provenance": provenance,
        "sha256": digest(raw),
    }


def validate_api_base(value: str, option: str = "--base-url") -> str:
    """Validate an explicit API root without changing its mapping identity."""
    try:
        if not isinstance(value, str) or any(char.isspace() or ord(char) < 32 for char in value):
            raise ValueError("invalid URL")
        parsed = urllib.parse.urlparse(value)
        port = parsed.port
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.params
                or (port is not None and not 1 <= port <= 65535)):
            raise ValueError("invalid API root")
    except (TypeError, ValueError):
        raise ImportError_(f"{option} must be an HTTP(S) API root without credentials, whitespace, query or fragment") from None
    return value.rstrip("/")


class _NoApiRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        # Even an explicitly chosen transport is not authority for another URL.
        return None


class RagflowApi:
    def __init__(self, base_url: str, api_key: str, *, transport_base_url: str | None = None):
        self._base = validate_api_base(base_url)
        self._transport_base = (self._base if transport_base_url is None else
                                validate_api_base(transport_base_url, "--transport-base-url"))
        # Do not route the local-service credential through an inherited proxy.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoApiRedirect())
        self.api_key = api_key

    @property
    def base(self) -> str:
        """Canonical mapping namespace; never temporarily rewritten for HTTP."""
        return self._base

    @property
    def transport_base(self) -> str:
        """Explicit physical API root; does not imply alias/ownership equivalence."""
        return self._transport_base

    def _json(self, method: str, path: str, body: dict | None = None) -> dict:
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or "#" in path or any(c.isspace() or ord(c) < 32 for c in path):
            raise ImportError_("RAGFlow request path must be relative to the selected API root")
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self.api_key}"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.transport_base + path, data=payload, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=120) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise ImportError_(f"RAGFlow HTTP {status} on {method} {path}") from None
        except urllib.error.URLError:
            raise ImportError_("RAGFlow transport unavailable") from None
        except (ValueError, UnicodeError) as exc:
            raise ImportError_(f"RAGFlow returned invalid JSON on {method} {path}") from exc
        if not isinstance(result, dict) or result.get("code") != 0:
            raise ImportError_(f"RAGFlow rejected {method} {path}")
        return result.get("data")

    def _document_path(self, dataset_id: str, document_id: str | None = None) -> str:
        path = f"/datasets/{urllib.parse.quote(dataset_id, safe='')}/documents"
        if document_id is not None:
            path += "/" + urllib.parse.quote(document_id, safe="")
        return path

    def find_document(self, dataset_id: str, name: str) -> dict | None:
        path = self._document_path(dataset_id)
        page = 1
        matches = []
        while True:
            query = urllib.parse.urlencode({"keywords": name, "page": page, "page_size": 100})
            data = self._json("GET", f"{path}?{query}")
            if not isinstance(data, dict) or not isinstance(data.get("docs"), list):
                raise ImportError_("RAGFlow document list has an unexpected shape")
            batch = data["docs"]
            matches.extend(doc for doc in batch if doc.get("name") == name)
            if page * 100 >= data.get("total", 0) or not batch:
                break
            page += 1
        if len(matches) > 1:
            raise ImportError_(f"More than one RAGFlow document has name {name}")
        return matches[0] if matches else None

    def create_document(self, dataset_id: str, name: str) -> dict:
        data = self._json("POST", self._document_path(dataset_id) + "?type=empty", {"name": name})
        if not isinstance(data, dict) or not data.get("id"):
            raise ImportError_("RAGFlow did not return the virtual document ID")
        return data

    def update_metadata(self, dataset_id: str, document_id: str, fields: dict) -> None:
        self._json("PATCH", self._document_path(dataset_id, document_id), {"meta_fields": fields})

    def list_chunks(self, dataset_id: str, document_id: str) -> dict:
        path = self._document_path(dataset_id, document_id) + "/chunks"
        page = 1
        chunks = []
        doc = None
        total = None
        while True:
            query = urllib.parse.urlencode({"page": page, "page_size": 100})
            data = self._json("GET", f"{path}?{query}")
            if not isinstance(data, dict) or not isinstance(data.get("chunks"), list):
                raise ImportError_("RAGFlow chunk list has an unexpected shape")
            chunks.extend(data["chunks"])
            doc, total = data.get("doc"), data.get("total")
            if not isinstance(total, int):
                raise ImportError_("RAGFlow chunk list has no total")
            if page * 100 >= total or not data["chunks"]:
                break
            page += 1
        if len(chunks) != total:
            raise ImportError_("RAGFlow chunk pagination changed during the read; retry later")
        if len({chunk.get("id") for chunk in chunks}) != len(chunks):
            raise ImportError_("RAGFlow returned repeated chunk IDs")
        return {"chunks": chunks, "doc": doc, "total": total}

    def add_chunk(self, dataset_id: str, document_id: str, content: str) -> dict:
        path = self._document_path(dataset_id, document_id) + "/chunks"
        data = self._json("POST", path, {"content": content})
        chunk = data.get("chunk") if isinstance(data, dict) else None
        if not isinstance(chunk, dict) or not chunk.get("id") or chunk.get("content") != content:
            raise ImportError_("RAGFlow Add chunk returned an unexpected chunk")
        return chunk

    def delete_chunk(self, dataset_id: str, document_id: str, chunk_id: str) -> None:
        path = self._document_path(dataset_id, document_id) + "/chunks"
        self._json("DELETE", path, {"chunk_ids": [chunk_id]})

    def retrieve(self, request: dict) -> dict:
        data = self._json("POST", "/retrieval", request)
        if not isinstance(data, dict) or not isinstance(data.get("chunks"), list):
            raise ImportError_("RAGFlow retrieval has an unexpected shape")
        return data


def state_path_for(state_dir: Path, base: str, dataset_id: str, source_document_id: str) -> Path:
    scope = f"{base.rstrip('/')}\n{dataset_id}\n{source_document_id}".encode("utf-8")
    return state_dir / f"{digest(scope)[:24]}.json"


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # A PermissionError can make Windows tempfile retry many random names when
    # os.access reports writable despite an effective ACL/sandbox denial. One
    # exclusive UUID creation fails promptly instead; never retry permission.
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    descriptor = None
    created = False
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0),
                             0o600)
        created = True
        stream = os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n')
        descriptor = None  # The stream now owns the descriptor.
        with stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if created and temporary.exists():
            temporary.unlink()


def load_state(path: Path, base: str, dataset_id: str, bundle: dict) -> dict:
    expected = {"importer": IMPORTER_MARKER, "base_url": base.rstrip("/"),
                "dataset_id": dataset_id, "source_document_id": bundle["document_id"],
                "document_name": bundle["document_name"]}
    if not path.exists():
        return {**expected, "ragflow_document_id": None, "creation_attempted": False,
                "entries": {}, "completed_source_sha256": None, "import_in_progress": False}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ImportError_(f"Cannot read import state {path}: {exc}") from exc
    if not isinstance(state, dict) or any(state.get(key) != value for key, value in expected.items()):
        raise ImportError_(f"Import state scope does not match {path}")
    if not isinstance(state.get("entries"), dict):
        raise ImportError_("Import state lacks the chunk mapping")
    return state


def paper_metadata(bundle: dict) -> dict:
    provenance = bundle["provenance"]
    fields = {"codex_importer": IMPORTER_MARKER,
              "project_document_id": bundle["document_id"],
              "article_title": bundle["title"]}
    for key in ("source_pdf_sha256", "resource_type", "source_xml_sha256", "source_parser",
                "source", "endpoint", "permission_evidence", "jats_product_evidence",
                "zotero_parent_key", "zotero_pdf_attachment_key", "zotero_attachment_key", "doi",
                "source_database", "source_id", "publication_date", "publication_date_source",
                "source_docx_sha256", "source_document_xml_sha256", "source_url",
                "source_role", "parent_document_id", "pmcid", "pmid", "license_code"):
        value = provenance.get(key)
        if isinstance(value, (str, int, float)) and value:
            fields[key] = value
    return fields


def check_owned(doc: dict, state: dict, bundle: dict) -> bool:
    if doc.get("type") != "virtual" or not doc.get("id"):
        raise ImportError_("Matching RAGFlow document is not a virtual document")
    if state["ragflow_document_id"] and state["ragflow_document_id"] != doc["id"]:
        raise ImportError_("Matching RAGFlow document ID differs from saved import state")
    meta = doc.get("meta_fields") or {}
    if meta.get("codex_importer") == IMPORTER_MARKER and meta.get("project_document_id") == bundle["document_id"]:
        return True
    if not meta and state["creation_attempted"] and not state["entries"]:
        return False  # interrupted between virtual document creation and metadata PATCH
    raise ImportError_("Matching virtual document is not marked as owned by this importer")


def reconcile(api, dataset_id: str, bundle: dict, state_path: Path,
              *, reindex: bool = False, verify_document_counter: bool = True) -> dict:
    state = load_state(state_path, api.base, dataset_id, bundle)
    # Optional bounded client checks the exact source/mapping before any local
    # intent write. Legacy clients retain their original behavior and gates.
    scope_validator = getattr(api, "validate_import_scope", None)
    if scope_validator is not None:
        if not callable(scope_validator):
            raise ImportError_("Bound import scope validator is invalid")
        scope_validator(dataset_id, bundle, state, reindex=reindex,
                        verify_document_counter=verify_document_counter)
    state["import_in_progress"] = True
    state["completed_source_sha256"] = None
    save_state(state_path, state)  # record intent before the first API mutation
    doc = api.find_document(dataset_id, bundle["document_name"])
    if doc is None:
        if state["ragflow_document_id"] or state["entries"]:
            raise ImportError_("Previously imported virtual document is missing; inspect before recreating")
        state["creation_attempted"] = True
        save_state(state_path, state)
        doc = api.create_document(dataset_id, bundle["document_name"])
        state["ragflow_document_id"] = doc["id"]
        save_state(state_path, state)
    owned = check_owned(doc, state, bundle)
    document_id = doc["id"]
    if not owned:
        empty = api.list_chunks(dataset_id, document_id)
        if empty["total"]:
            raise ImportError_("Unmarked virtual document contains chunks; refusing to adopt it")
        api.update_metadata(dataset_id, document_id, paper_metadata(bundle))
        marked = api.find_document(dataset_id, bundle["document_name"])
        if marked is None or not check_owned(marked, state, bundle):
            raise ImportError_("RAGFlow did not persist the importer ownership metadata")
    state["ragflow_document_id"] = document_id
    save_state(state_path, state)

    listing = api.list_chunks(dataset_id, document_id)
    remote = {}
    desired = bundle["by_content"]
    for chunk in listing["chunks"]:
        chunk_id, content = chunk.get("id"), chunk.get("content")
        if not isinstance(chunk_id, str) or not isinstance(content, str):
            raise ImportError_("RAGFlow returned a chunk without ID/content")
        old = state["entries"].get(chunk_id)
        if old is not None and old.get("content_sha256") != text_digest(content):
            raise ImportError_(f"Saved mapping disagrees with RAGFlow chunk {chunk_id}")
        if old is None and content not in desired:
            raise ImportError_(f"Unmapped RAGFlow chunk {chunk_id}; refusing to delete or overwrite it")
        if content in remote:
            raise ImportError_("RAGFlow has duplicate chunk content within this document")
        remote[content] = chunk_id
        if content in desired:
            state["entries"][chunk_id] = {"content_sha256": text_digest(content),
                                           "record": desired[content]}
    for chunk_id in list(state["entries"]):
        if chunk_id not in remote.values():
            del state["entries"][chunk_id]  # a chunk was removed externally
    save_state(state_path, state)

    removed = 0
    if reindex:
        for content, chunk_id in list(remote.items()):
            api.delete_chunk(dataset_id, document_id, chunk_id)
            del remote[content]
            state["entries"].pop(chunk_id, None)
            save_state(state_path, state)
            removed += 1

    added = 0
    for row in bundle["rows"]:
        content = row["embedding_text"]
        if content in remote:
            continue
        chunk = api.add_chunk(dataset_id, document_id, content)
        remote[content] = chunk["id"]
        state["entries"][chunk["id"]] = {"content_sha256": text_digest(content), "record": row}
        save_state(state_path, state)
        added += 1

    for content, chunk_id in list(remote.items()):
        if content in desired:
            continue
        api.delete_chunk(dataset_id, document_id, chunk_id)
        del remote[content]
        state["entries"].pop(chunk_id, None)
        save_state(state_path, state)
        removed += 1

    verified = api.list_chunks(dataset_id, document_id)
    actual = {chunk.get("content"): chunk.get("id") for chunk in verified["chunks"]}
    if actual != remote or set(actual) != set(desired) or verified["total"] != len(desired):
        raise ImportError_("RAGFlow read-back does not match the requested JSONL chunks")
    doc_count = (verified.get("doc") or {}).get("chunk_count")
    if verify_document_counter and doc_count is not None and doc_count != len(desired):
        raise ImportError_(f"RAGFlow document chunk counter is {doc_count}, expected {len(desired)}")
    api.update_metadata(dataset_id, document_id, paper_metadata(bundle))
    state["completed_source_sha256"] = bundle["sha256"]
    state["import_in_progress"] = False
    save_state(state_path, state)
    return {"document_id": document_id, "chunks": len(desired), "added": added,
            "removed": removed, "mapping_path": str(state_path),
            "document_chunk_counter": doc_count,
            "document_counter_verified": verify_document_counter}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chunks", required=True, type=Path, help="One paper's standard JSONL")
    parser.add_argument("--dataset-id", help="RAGFlow dataset ID; required with --execute")
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--transport-base-url", help="Explicit HTTP API root; --base-url still selects mapping ownership")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--execute", action="store_true", help="Write to RAGFlow")
    parser.add_argument("--reindex", action="store_true", help="Rebuild only this managed paper's chunks")
    args = parser.parse_args(argv)
    try:
        bundle = load_bundle(args.chunks)
        if not args.execute:
            if args.reindex:
                raise ImportError_("--reindex requires --execute")
            print(json.dumps({"mode": "dry-run", "document_name": bundle["document_name"],
                              "title": bundle["title"], "chunks": len(bundle["rows"]),
                              "source_sha256": bundle["sha256"]}, ensure_ascii=False))
            return 0
        if not args.dataset_id:
            raise ImportError_("--execute requires --dataset-id")
        key = os.environ.get("RAGFLOW_API_KEY", "")
        if not key:
            raise ImportError_("--execute requires RAGFLOW_API_KEY in the environment")
        api = RagflowApi(args.base_url, key, transport_base_url=args.transport_base_url)
        state_path = state_path_for(args.state_dir, api.base, args.dataset_id, bundle["document_id"])
        result = reconcile(api, args.dataset_id, bundle, state_path, reindex=args.reindex)
        print(json.dumps({"mode": "execute", **result}, ensure_ascii=False))
        return 0
    except (ImportError_, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
