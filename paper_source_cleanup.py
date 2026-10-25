"""Post-index source cleanup; preview by default, with durable recovery first.

Only a tracked main PDF is removed. Supplements, JSON, manifests, task files,
and Zotero originals are never deleted by this module.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from download_ledger import DownloadLedger, LedgerConflict, normalized_doi
from process_lock import process_lock
from zotero_filing import (DEFAULT_STORAGE, FilingError, _file_identity, _json,
                           _key, _source_file)
from zotero_preflight import normalize_title
from zotero_topics import collection_path


class CleanupBlocked(RuntimeError):
    pass


def _need(condition, code):
    if not condition:
        raise CleanupBlocked(code)


def _gate(root, job_key, client, storage_root, prepared_root, state_dir,
          base_url, dataset_id, api):
    ledger = DownloadLedger(root)
    job = _json(root / ".paper-download-jobs.json")["jobs"].get(job_key, {})
    _need(job.get("status") == "SAVED", "JOB_NOT_SAVED")
    rows = [ledger.state["entries"][identifier] for identifier in job.get("identifiers", [])
            if identifier in ledger.state["entries"]]
    _need(rows and all(row == rows[0] for row in rows), "LEDGER_CONFLICT")
    entry = rows[0]
    _need(entry.get("cleanup_policy", {}).get("enabled") is True,
          "LEGACY_OR_PROTECTED_SOURCE_RETAINED")
    _need(not job.get("retain_source") and not entry.get("retain_source"), "SOURCE_PROTECTED")
    _need(job.get("pdf_filename") == entry.get("filename") and
          job.get("pdf_sha256") == entry.get("sha256") and
          job.get("pdf_size_bytes") == entry.get("size_bytes"), "JOB_LEDGER_CONFLICT")
    source = ledger.source_path(entry)
    pdf, size, digest = ledger._verify_entry(entry)
    filed_state = _json(root / ".paper-zotero-filing.json")
    filed = filed_state["jobs"].get(job_key, {})
    _need(filed.get("status") == "FILED" and not filed.get("zotero_write_uncertain")
          and filed.get("source_pdf_sha256") == digest, "ZOTERO_NOT_VERIFIED")
    _need(filed.get("topic_keyword") and filed.get("collection_path"), "TOPIC_RECEIPT_REQUIRED")
    client.discover()  # read only; never ask for write authorization here
    _need(client.server_id == filed.get("server_id") == filed_state.get("server_id"),
          "ZOTERO_INSTANCE_CHANGED")
    parent_key, attachment_key, collection_key = (
        _key(filed.get(field)) for field in ("parent_key", "pdf_attachment_key", "collection_key"))
    parent = client.get_json(f"/users/0/items/{parent_key}")["data"]
    doi = normalized_doi(entry.get("doi"))
    _need(doi is not None and normalized_doi(parent.get("DOI")) == doi, "DOI_NOT_CONFIRMED")
    _need(normalize_title(parent.get("title")) == normalize_title(job.get("title")),
          "TITLE_NOT_CONFIRMED")
    _need(not parent.get("deleted") and collection_key in parent.get("collections", []),
          "COLLECTION_NOT_CONFIRMED")
    tree = client.get_json("/users/0/collections?format=json")
    actual_path = collection_path(collection_key, tree)
    _need(actual_path == filed["collection_path"] and
          normalize_title(actual_path.split("/")[-1]) != normalize_title(parent.get("title")),
          "TOPIC_PATH_CHANGED")
    child = client.get_json(f"/users/0/items/{attachment_key}")["data"]
    _need(child.get("parentItem") == parent_key and not child.get("deleted")
          and child.get("linkMode") in {"imported_file", "imported_url"}, "PDF_IS_NOT_STORED_COPY")
    stored = _source_file(client, attachment_key, storage_root)
    stored_identity = _file_identity(stored)
    _need(not stored.is_relative_to(root) and stored_identity["sha256"] == digest
          and stored_identity["size"] == size, "STORED_PDF_CONTENT_DIFFERS")
    if source.exists():
        _need(not os.path.samefile(source, stored), "PDF_IS_NOT_INDEPENDENT_COPY")
    scripts = str(Path(__file__).resolve().parents[1] / "RAGFlow" / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from import_prepared_queue import _validated_prepared, _completed_mapping
    from mineru_v1_convert import _reuse_integrity
    from process_saved_queue import _prepared_is_current
    processing = _json(root / ".paper-processing-state.json")["jobs"].get(job_key)
    _need(isinstance(processing, dict) and processing.get("source_pdf_sha256") == digest,
          "MINERU_NOT_COMPLETED")
    bundle_dir = Path(processing.get("bundle_dir", ""))
    _need(bundle_dir.is_absolute(), "MINERU_BUNDLE_MISSING")
    _, integrity = _reuse_integrity(bundle_dir, pdf, digest, digest[:20])
    _need(integrity == "artifact_hashes_verified", "MINERU_ARTIFACT_HASHES_REQUIRED")
    _, prepared = _validated_prepared(processing, prepared_root)
    expected_zotero = {"zotero_parent_key": parent_key, "zotero_pdf_attachment_key": attachment_key,
                       "zotero_collection_key": collection_key, "zotero_server_id": client.server_id}
    _need(processing.get("zotero_provenance") == expected_zotero and _prepared_is_current(
        processing, {"pdf_sha256": digest}, Path(prepared_root), expected_zotero,
        expected_policy=processing.get("chunk_policy"),
        expected_paper_provenance=processing.get("paper_provenance")), "PREPARED_NOT_COMPLETE")
    _need(prepared["provenance"].get("source_pdf_sha256") == digest, "PREPARED_SOURCE_CHANGED")
    imported = _json(root / ".paper-ragflow-import-state.json")["jobs"].get(job_key, {})
    mapping = _completed_mapping(state_dir, base_url, dataset_id, prepared)
    _need(mapping is not None and imported.get("status") == "IMPORTED" and
          imported.get("dataset_id") == dataset_id and imported.get("base_url") == base_url.rstrip("/")
          and imported.get("source_pdf_sha256") == digest and
          imported.get("chunks_sha256") == prepared["sha256"] and
          imported.get("ragflow_document_id") == mapping["state"]["ragflow_document_id"],
          "INDEX_NOT_COMPLETED")
    _need(api is not None and api.base.rstrip("/") == base_url.rstrip("/"), "INDEX_READBACK_REQUIRED")
    listing = api.list_chunks(dataset_id, imported["ragflow_document_id"])
    chunks = listing.get("chunks", [])
    _need(Counter(row.get("content") for row in chunks) ==
          Counter(row["embedding_text"] for row in prepared["rows"]), "INDEX_READBACK_DIFFERS")
    return ledger, entry, source, stored, filed, digest, size, actual_path


def cleanup_saved_job(directory, job_key, *, client, prepared_root, state_dir,
                      base_url, dataset_id, api=None, storage_root=DEFAULT_STORAGE,
                      execute=False):
    root = Path(directory).resolve(strict=True)
    source_removed = False

    def run():
        nonlocal source_removed
        ledger, entry, source, stored, filed, digest, size, path = _gate(
            root, job_key, client, storage_root, prepared_root, state_dir, base_url, dataset_id, api)
        answer = {"job_key": job_key, "status": "READY_TO_CLEAN", "source_path": str(source),
                  "recovery_path": str(stored), "sha256": digest, "collection_path": path}
        if not execute:
            return answer
        aliases = [key for key, row in ledger.state["entries"].items() if row == entry]
        entry["recovery"] = {
            "status": "CLEANUP_PENDING", "locations": [{"kind": "zotero_stored", "path": str(stored),
                "server_id": filed["server_id"], "parent_key": filed["parent_key"],
                "attachment_key": filed["pdf_attachment_key"]}],
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "dataset_id": dataset_id, "base_url": base_url.rstrip("/"),
        }
        for key in aliases:
            ledger.state["entries"][key] = entry
        # Write every identifier alias before unlinking. A crash on either side
        # can resume through the exact same hash-verified stored original.
        ledger._save()
        if source.exists():
            ledger._regular_chain(source)
            _need(source.resolve().is_relative_to(root), "SOURCE_OUTSIDE_DOWNLOAD_DIRECTORY")
            for target in (source, stored):
                current = _file_identity(target)
                _need(current["sha256"] == digest and current["size"] == size, "PDF_CHANGED_BEFORE_CLEANUP")
            source.unlink()  # exact main PDF only; never recursive
        source_removed = True
        entry["recovery"]["status"] = "SOURCE_REMOVED"
        ledger._save()
        ledger._verify_entry(entry)  # recovery must work with the source absent
        answer["status"] = "SOURCE_REMOVED"
        return answer

    try:
        if execute:
            with process_lock(Path(__file__).with_name("paper_downloader-global")):
                return run()
        return run()
    except (CleanupBlocked, FilingError) as exc:
        return {"job_key": job_key, "status": "RECOVERY_PENDING" if source_removed else "RETAINED",
                "reason_code": str(exc)}
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        return {"job_key": job_key, "status": "RECOVERY_PENDING" if source_removed else "RETAINED",
                "reason_code": "CLEANUP_VERIFICATION_INCOMPLETE"}


def refresh_original_location(directory, job_key, *, client, storage_root=DEFAULT_STORAGE):
    """Find a renamed/moved stored attachment by Zotero keys; never mutate Zotero."""
    root = Path(directory).resolve(strict=True)
    with process_lock(Path(__file__).with_name("paper_downloader-global")):
        ledger = DownloadLedger(root)
        job = _json(root / ".paper-download-jobs.json")["jobs"][job_key]
        aliases = [key for key in job.get("identifiers", []) if key in ledger.state["entries"]]
        _need(aliases, "LEDGER_CONFLICT")
        entry = ledger.state["entries"][aliases[0]]
        _need(all(ledger.state["entries"][key] == entry for key in aliases), "LEDGER_CONFLICT")
        aliases = [key for key, row in ledger.state["entries"].items() if row == entry]
        recovery = entry.get("recovery", {})
        locations = recovery.get("locations", [])
        client.discover()
        for location in locations:
            if location.get("kind") != "zotero_stored" or location.get("server_id") != client.server_id:
                continue
            key, parent = _key(location.get("attachment_key")), _key(location.get("parent_key"))
            data = client.get_json(f"/users/0/items/{key}")["data"]
            _need(data.get("parentItem") == parent and not data.get("deleted") and
                  data.get("linkMode") in {"imported_file", "imported_url"}, "PDF_IS_NOT_STORED_COPY")
            pdf = _source_file(client, key, storage_root)
            identity = _file_identity(pdf)
            _need(identity["sha256"] == entry.get("sha256") and
                  identity["size"] == entry.get("size_bytes"), "STORED_PDF_CONTENT_DIFFERS")
            _need(not pdf.is_relative_to(root), "PDF_IS_NOT_INDEPENDENT_COPY")
            location["path"] = str(pdf)
            for alias in aliases:
                ledger.state["entries"][alias] = entry
            ledger._save()
            return {"job_key": job_key, "status": "ORIGINAL_LOCATED", "pdf_path": str(pdf),
                    "sha256": identity["sha256"], "pdf_attachment_key": key}
        raise CleanupBlocked("RECOVERABLE_ZOTERO_ATTACHMENT_NOT_FOUND")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download-dir", type=Path, required=True)
    parser.add_argument("--job-key", required=True)
    parser.add_argument("--prepared-root", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--dataset-id")
    parser.add_argument("--storage-root", type=Path, default=DEFAULT_STORAGE)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--recover-original", action="store_true",
                        help="refresh the local recovery path by Zotero attachment key; no source deletion")
    args = parser.parse_args(argv)
    from zotero_local_api import ZoteroLocalClient
    if args.recover_original:
        try:
            result = refresh_original_location(args.download_dir, args.job_key,
                client=ZoteroLocalClient(), storage_root=args.storage_root)
        except (CleanupBlocked, FilingError, OSError, ValueError, RuntimeError, KeyError, TypeError):
            result = {"job_key": args.job_key, "status": "RETAINED", "reason_code": "ORIGINAL_RECOVERY_INCOMPLETE"}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "ORIGINAL_LOCATED" else 2
    if not all((args.prepared_root, args.state_dir, args.base_url, args.dataset_id)):
        parser.error("cleanup requires --prepared-root, --state-dir, --base-url and --dataset-id")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "RAGFlow" / "scripts"))
    from ragflow_prechunk_import import RagflowApi
    # A missing read credential retains sources; it never triggers an upload.
    key = os.environ.get("RAGFLOW_API_KEY")
    api = RagflowApi(args.base_url, key) if key else None
    result = cleanup_saved_job(args.download_dir, args.job_key, client=ZoteroLocalClient(),
        prepared_root=args.prepared_root, state_dir=args.state_dir, base_url=args.base_url,
        dataset_id=args.dataset_id, storage_root=args.storage_root, execute=args.execute, api=api)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"READY_TO_CLEAN", "SOURCE_REMOVED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
