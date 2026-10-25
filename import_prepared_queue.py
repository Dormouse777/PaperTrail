#!/usr/bin/env python3
"""Import a bounded batch of PREPARED queue jobs into RAGFlow safely.

Preview is the default and never reads credentials or contacts RAGFlow. Online
execution requires --execute, --confirm-ragflow-write, a dataset ID, and a key
in RAGFLOW_API_KEY. Per-job failures are isolated and no Zotero state is changed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from handoff_saved_download import (HANDOFF_FILE, HandoffError, _load_object,
                                   verify_saved_job, read_filed_zotero_provenance)
from job_scope import validate_job_scope
from ragflow_prechunk_import import (
    DEFAULT_BASE,
    DEFAULT_STATE_DIR,
    IMPORTER_MARKER,
    ImportError_,
    RagflowApi,
    load_bundle,
    load_state,
    reconcile,
    state_path_for,
)


IMPORT_STATE_FILE = ".paper-ragflow-import-state.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, value: dict) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent,
            prefix=path.name + ".", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _load_optional_state(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "jobs": {}}
    return _load_object(path, "RAGFlow import state")


def _validated_prepared(record: object, prepared_root: Path) -> tuple[Path, dict]:
    if not isinstance(record, dict) or record.get("status") != "PREPARED":
        raise HandoffError("Processing record is not PREPARED")
    raw_path = record.get("chunks_path")
    if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
        raise HandoffError("PREPARED record has no absolute chunks path")
    path = Path(raw_path)
    try:
        resolved = path.resolve(strict=True)
        allowed = Path(prepared_root).resolve(strict=True)
    except OSError as exc:
        raise HandoffError("Prepared chunks path is unavailable") from exc
    if path.is_symlink() or not resolved.is_file() or not resolved.is_relative_to(allowed):
        raise HandoffError("Prepared chunks are outside the allowed root")
    bundle = load_bundle(resolved)
    expected_pdf = str(record.get("source_pdf_sha256", "")).lower()
    actual_pdf = str(bundle["provenance"].get("source_pdf_sha256", "")).lower()
    if expected_pdf != actual_pdf:
        raise HandoffError("Prepared chunks do not match the handoff PDF hash")
    if record.get("chunk_count") != len(bundle["rows"]):
        raise HandoffError("Prepared chunk count differs from the handoff state")
    return resolved, bundle


def _completed_mapping(state_dir: Path, base_url: str, dataset_id: str,
                       bundle: dict) -> dict | None:
    path = state_path_for(state_dir, base_url, dataset_id, bundle["document_id"])
    if not path.is_file() or path.is_symlink():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    if (state.get("importer") != IMPORTER_MARKER or
            state.get("base_url") != base_url.rstrip("/") or
            state.get("dataset_id") != dataset_id or
            state.get("source_document_id") != bundle["document_id"] or
            state.get("completed_source_sha256") != bundle["sha256"] or
            state.get("import_in_progress") is not False or
            not isinstance(state.get("ragflow_document_id"), str) or
            len(state.get("entries", {})) != len(bundle["rows"])):
        return None
    return {"path": path, "state": state}


def _verify_filed_prepared(root, job_key, record, bundle):
    verified = verify_saved_job(root, job_key)
    binding = read_filed_zotero_provenance(root, job_key, verified["pdf_sha256"])
    if binding is None or record.get("zotero_provenance") != binding:
        raise HandoffError("Current FILED binding is missing or differs from PREPARED state")
    for row in bundle["rows"]:
        provenance = row.get("provenance", {})
        if (provenance.get("source_pdf_sha256") != verified["pdf_sha256"]
                or any(provenance.get(key) != value for key, value in binding.items())):
            raise HandoffError("Prepared chunks differ from the current FILED source")


def inspect_prepared_queue(download_dir: Path, prepared_root: Path, state_dir: Path,
                           base_url: str, dataset_id: str, *, maximum: int = 1,
                           job_keys=None, require_filed: bool = False) -> dict:
    scope = validate_job_scope(job_keys)
    if type(require_filed) is not bool or require_filed and scope is None:
        raise HandoffError("require_filed requires an explicit job scope")
    if type(maximum) is not int or not 1 <= maximum <= 10:
        raise HandoffError("maximum must be an integer from 1 to 10")
    if not isinstance(dataset_id, str) or not dataset_id.strip():
        raise HandoffError("dataset_id is required for scoped import inspection")
    root = Path(download_dir).resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise HandoffError("Download directory must be a regular directory")
    processing_path = root / HANDOFF_FILE
    if not processing_path.exists():
        return {
            "download_dir": str(root),
            "queue_initialized": False,
            "prepared_jobs": 0,
            "already_imported": [],
            "ready": [],
            "selected": [],
            "deferred_by_limit": 0,
            "invalid": [],
        }
    processing = _load_object(processing_path, "processing state")
    records = processing.get("jobs")
    if not isinstance(records, dict):
        raise HandoffError("Processing state jobs must be an object")
    import_state = _load_optional_state(root / IMPORT_STATE_FILE)
    imported_records = import_state.get("jobs")
    if not isinstance(imported_records, dict):
        raise HandoffError("RAGFlow import state jobs must be an object")

    ready, already_imported, invalid = [], [], []
    for job_key in sorted(records):
        if scope is not None and job_key not in scope:
            continue
        try:
            chunks_path, bundle = _validated_prepared(records[job_key], prepared_root)
            if require_filed:
                _verify_filed_prepared(root, job_key, records[job_key], bundle)
        except (HandoffError, ImportError_, OSError, ValueError) as exc:
            invalid.append({"job_key": job_key, "error": str(exc)})
            continue
        item = {
            "job_key": job_key,
            "document_id": bundle["document_id"],
            "title": bundle["title"],
            "chunks": len(bundle["rows"]),
            "chunks_path": str(chunks_path),
            "chunks_sha256": bundle["sha256"],
        }
        completed = _completed_mapping(state_dir, base_url, dataset_id, bundle)
        saved = imported_records.get(job_key)
        if (completed is not None and isinstance(saved, dict) and
                saved.get("status") == "IMPORTED" and
                saved.get("dataset_id") == dataset_id and
                saved.get("base_url") == base_url.rstrip("/") and
                saved.get("chunks_sha256") == bundle["sha256"] and
                saved.get("ragflow_document_id") == completed["state"]["ragflow_document_id"]):
            already_imported.append(item)
        else:
            ready.append(item)
    selected = ready[:maximum]
    return {
        "download_dir": str(root),
        "queue_initialized": True,
        "prepared_jobs": len(records),
        "already_imported": already_imported,
        "ready": ready,
        "selected": selected,
        "deferred_by_limit": len(ready) - len(selected),
        "invalid": invalid,
        **({"job_keys": sorted(scope), "scoped_out_jobs": sum(key not in scope for key in records)}
           if scope is not None else {}),
    }


def import_prepared_queue(download_dir: Path, prepared_root: Path, state_dir: Path,
                          base_url: str, dataset_id: str, *, maximum: int = 1,
                          execute: bool = False, confirm_ragflow_write: bool = False,
                          api=None, clock=_utc_now, job_keys=None,
                          require_filed: bool = False, halt_on_failure: bool = False) -> dict:
    if type(halt_on_failure) is not bool:
        raise HandoffError("halt_on_failure must be boolean")
    if execute and not confirm_ragflow_write:
        raise HandoffError("execute requires explicit confirm_ragflow_write")
    queue = inspect_prepared_queue(
        download_dir, prepared_root, state_dir, base_url, dataset_id, maximum=maximum,
        job_keys=job_keys, require_filed=require_filed,
    )
    if not execute:
        return {"mode": "dry-run", **queue, "ragflow_write": False,
                "ragflow_write_attempted": False,
                "ragflow_write_uncertain": False,
                "zotero_write": False, "processed": [], "failures": queue["invalid"]}
    if api is None:
        raise HandoffError("An initialized RAGFlow API client is required for execute")
    if api.base != base_url.rstrip("/"):
        raise HandoffError("RAGFlow API client base does not match the inspected scope")

    root = Path(download_dir).resolve(strict=True)
    import_path = root / IMPORT_STATE_FILE
    import_state = _load_optional_state(import_path)
    jobs = import_state.setdefault("jobs", {})
    processed, failures = [], list(queue["invalid"])
    write_attempted = False
    write_uncertain = False
    for item in queue["selected"]:
        if halt_on_failure and failures:
            if callable(getattr(type(api), 'abort_import_session', None)):
                api.abort_import_session()
            break
        item_write_attempted = False
        try:
            bundle = load_bundle(Path(item["chunks_path"]))
            if require_filed:
                current = _load_object(root / HANDOFF_FILE, "processing state")
                record = current.get("jobs", {}).get(item["job_key"])
                current_path, current_bundle = _validated_prepared(record, prepared_root)
                if current_path != Path(item["chunks_path"]) or current_bundle["sha256"] != bundle["sha256"]:
                    raise HandoffError("PREPARED record changed before import")
                _verify_filed_prepared(root, item["job_key"], record, bundle)
            mapping_path = state_path_for(
                state_dir, api.base, dataset_id, bundle["document_id"],
            )
            if callable(getattr(type(api), 'bind_prepared_job', None)):
                state = load_state(mapping_path, api.base, dataset_id, bundle)
                api.bind_prepared_job(root, item['job_key'], dataset_id, bundle, state,
                                      require_filed=require_filed)
            # reconcile may mutate RAGFlow before a later request/read-back fails.
            item_write_attempted = True
            write_attempted = True
            result = reconcile(api, dataset_id, bundle, mapping_path)
            jobs[item["job_key"]] = {
                "status": "IMPORTED",
                "dataset_id": dataset_id,
                "base_url": api.base,
                "source_pdf_sha256": bundle["provenance"]["source_pdf_sha256"],
                "chunks_sha256": bundle["sha256"],
                "document_id": bundle["document_id"],
                "ragflow_document_id": result["document_id"],
                "chunk_count": result["chunks"],
                "mapping_path": result["mapping_path"],
                "updated_at": clock(),
            }
            _atomic_json(import_path, import_state)
            processed.append({"job_key": item["job_key"], **result})
        except (HandoffError, ImportError_, OSError, ValueError, RuntimeError) as exc:
            if item_write_attempted:
                write_uncertain = True
            failures.append({
                "job_key": item["job_key"], "error": str(exc),
                "ragflow_write_uncertain": item_write_attempted,
            })
            if callable(getattr(type(api), 'abort_import_session', None)):
                api.abort_import_session()
                break  # A failed grant/source must not cause more requests in this session.
            if halt_on_failure:
                break
    return {
        "mode": "execute", **queue, "processed": processed,
        "failures": failures, "ragflow_write": bool(processed),
        "ragflow_write_attempted": write_attempted,
        "ragflow_write_uncertain": write_uncertain,
        "paused_requires_review": bool(halt_on_failure and (failures or write_uncertain)),
        "zotero_write": False, "import_state": str(import_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download-dir", type=Path, required=True)
    parser.add_argument("--prepared-root", type=Path,
                        default=Path(__file__).resolve().parents[1] / "artifacts" / "prepared")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--transport-base-url", help="Explicit HTTP API root; mapping identity stays --base-url")
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--max", type=int, default=1, dest="maximum")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-ragflow-write", action="store_true")
    args = parser.parse_args(argv)
    try:
        api = None
        if args.execute:
            if not args.confirm_ragflow_write:
                raise HandoffError("--execute requires --confirm-ragflow-write")
            key = os.environ.get("RAGFLOW_API_KEY", "")
            if not key:
                raise HandoffError("--execute requires RAGFLOW_API_KEY in the environment")
            api = RagflowApi(args.base_url, key, transport_base_url=args.transport_base_url)
        result = import_prepared_queue(
            args.download_dir, args.prepared_root, args.state_dir,
            args.base_url, args.dataset_id, maximum=args.maximum,
            execute=args.execute, confirm_ragflow_write=args.confirm_ragflow_write,
            api=api,
        )
    except (HandoffError, ImportError_, OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
