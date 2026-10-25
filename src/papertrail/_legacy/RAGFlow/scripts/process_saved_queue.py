#!/usr/bin/env python3
"""Process a bounded queue of verified SAVED download jobs locally.

The queue is scoped to one explicit download directory. Preview is the default,
and --max is capped at 10. Jobs already backed by a valid PREPARED artifact are
skipped. Execution delegates each selected job to handoff_saved_download.py and
isolates per-job failures. This command never writes Zotero or RAGFlow.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from handoff_saved_download import (
    HANDOFF_FILE,
    JOBS_FILE,
    LEDGER_FILE,
    HandoffError,
    _load_object,
    handoff,
    read_filed_zotero_provenance,
    verify_saved_job,
)
from workflow_configuration import default_path
from local_pdf_pipeline import build_chunk_policy
from prepare_chunks import PreparationError, validate_paper_provenance
from ragflow_prechunk_import import ImportError_, load_bundle
from job_scope import validate_job_scope


def _load_processing_state(root: Path) -> dict:
    path = root / HANDOFF_FILE
    if not path.exists():
        return {"version": 1, "jobs": {}}
    return _load_object(path, "processing state")


def _prepared_is_current(record: object, verified: dict, prepared_root: Path,
                         zotero_provenance: dict[str, str] | None = None, *,
                         expected_policy: dict | None = None,
                         expected_paper_provenance: dict | None = None) -> bool:
    if not isinstance(record, dict) or record.get("status") != "PREPARED":
        return False
    if str(record.get("source_pdf_sha256", "")).lower() != verified["pdf_sha256"].lower():
        return False
    if record.get("zotero_provenance") != zotero_provenance:
        return False
    raw_path = record.get("chunks_path")
    if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
        return False
    path = Path(raw_path)
    try:
        resolved = path.resolve(strict=True)
        allowed = Path(prepared_root).resolve(strict=True)
    except OSError:
        return False
    if path.is_symlink() or not resolved.is_file() or not resolved.is_relative_to(allowed):
        return False
    try:
        bundle = load_bundle(resolved)
    except (ImportError_, OSError, ValueError):
        return False
    manifest_path = resolved.parent / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        chunks_hash = hashlib.sha256(resolved.read_bytes()).hexdigest()
    except (OSError, UnicodeError, ValueError):
        return False
    manifest_source_hash = manifest.get("source_pdf_sha256") if isinstance(manifest, dict) else None
    if (not isinstance(manifest, dict)
            or manifest.get("status") != "PREPARED"
            or not isinstance(manifest_source_hash, str)
            or manifest_source_hash.lower() != verified["pdf_sha256"].lower()
            or manifest.get("chunks_sha256") != chunks_hash
            or manifest.get("chunk_count") != len(bundle["rows"])
            or manifest.get("document_id") != bundle["document_id"]):
        return False
    # None is reserved for identity/content-only consumers that did not
    # request rechunking (for example, read-only import/answer scope checks).
    # A saved queue inspection always supplies its concrete requested policy.
    if (expected_policy is not None
            and manifest.get("chunk_policy") != expected_policy):
        return False
    current_paper_provenance = None
    if expected_paper_provenance is not None:
        try:
            current_paper_provenance = validate_paper_provenance(manifest.get("paper_provenance"))
        except PreparationError:
            return False
        # Unknown metadata in a new request is not a request to discard a
        # previously validated, source-bound date or source label.
        if (current_paper_provenance is None
                or record.get("paper_provenance") != current_paper_provenance
                or any(current_paper_provenance.get(key) != value
                       for key, value in expected_paper_provenance.items() if value is not None)):
            return False
    if (record.get("fingerprint") is not None
            and record.get("fingerprint") != manifest.get("fingerprint")):
        return False
    for row in bundle["rows"]:
        provenance = row.get("provenance", {})
        source_hash = provenance.get("source_pdf_sha256")
        if not isinstance(source_hash, str) or source_hash.lower() != verified["pdf_sha256"].lower():
            return False
        if zotero_provenance is not None and any(
                provenance.get(key) != value
                for key, value in zotero_provenance.items()):
            return False
        if current_paper_provenance is not None and any(
                provenance.get(key) != value for key, value in current_paper_provenance.items()):
            return False
    return (len(bundle["rows"]) == record.get("chunk_count") and
            bundle["document_id"] in resolved.parts)


def inspect_queue(download_dir: Path, prepared_root: Path, *, maximum: int = 1,
                  job_keys=None, require_filed: bool = False,
                  target_tokens: int = 600, max_tokens: int = 900,
                  tokenizer_json: Path | None = None,
                  bibliography_by_job: dict | None = None) -> dict:
    try:
        chunk_policy = build_chunk_policy(
            target_tokens=target_tokens, max_tokens=max_tokens,
            tokenizer_json=tokenizer_json,
        )
    except PreparationError as exc:
        raise HandoffError(str(exc)) from exc
    scope = validate_job_scope(job_keys)
    if bibliography_by_job is not None and (type(bibliography_by_job) is not dict
            or scope is None or not set(bibliography_by_job) <= scope):
        raise HandoffError("Supplementary bibliography requires an explicit matching job scope")
    if type(require_filed) is not bool or require_filed and scope is None:
        raise HandoffError("require_filed requires an explicit job scope")
    if type(maximum) is not int or not 1 <= maximum <= 10:
        raise HandoffError("maximum must be an integer from 1 to 10")
    root = Path(download_dir).resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise HandoffError("Download directory must be a regular directory")
    jobs_path = root / JOBS_FILE
    ledger_path = root / LEDGER_FILE
    if not jobs_path.exists() and not ledger_path.exists():
        return {
            "download_dir": str(root), "queue_initialized": False,
            "saved_jobs": 0, "already_prepared": [], "ready": [],
            "selected": [], "deferred_by_limit": 0, "invalid": [],
            "chunk_policy": chunk_policy,
        }
    if jobs_path.exists() != ledger_path.exists():
        raise HandoffError("Download queue is incomplete: jobs and ledger must both exist")
    jobs_doc = _load_object(jobs_path, "download jobs")
    state = _load_processing_state(root)
    jobs = jobs_doc.get("jobs", {})
    if not isinstance(jobs, dict):
        raise HandoffError("Download jobs must be an object")
    state_jobs = state.get("jobs", {})
    if not isinstance(state_jobs, dict):
        raise HandoffError("Processing state jobs must be an object")
    saved_keys = sorted(
        (key for key, value in jobs.items()
         if isinstance(value, dict) and value.get("status") == "SAVED"),
        key=lambda key: (str(jobs[key].get("created_at", "")), key),
    )
    ready = []
    already_prepared = []
    invalid = []
    for key in saved_keys:
        if scope is not None and key not in scope:
            continue
        try:
            verified = verify_saved_job(root, key)
            zotero_provenance = read_filed_zotero_provenance(
                root, key, verified["pdf_sha256"])
            if require_filed and zotero_provenance is None:
                raise HandoffError("A verified FILED binding is required before local preparation")
            paper_provenance = None
            bibliography = (bibliography_by_job or {}).get(key)
            if verified.get("source") == "pubmed":
                from paper_provenance_projection import project_saved_paper_provenance
                paper_provenance = validate_paper_provenance(
                    project_saved_paper_provenance(root, key, bibliography=bibliography))
                if paper_provenance["source_pdf_sha256"] != verified["pdf_sha256"]:
                    raise HandoffError("Public paper provenance changed after verification")
            elif bibliography is not None:
                raise HandoffError("Supplementary bibliography requires a verified PubMed job")
        except (HandoffError, PreparationError, OSError, ValueError) as exc:
            invalid.append({"job_key": key, "error": str(exc)})
            continue
        item = {"job_key": key, "title": verified.get("title"),
                "source_pdf_sha256": verified["pdf_sha256"]}
        if _prepared_is_current(state_jobs.get(key), verified, prepared_root,
                                zotero_provenance, expected_policy=chunk_policy,
                                expected_paper_provenance=paper_provenance):
            already_prepared.append(item)
        else:
            ready.append(item)
    selected = ready[:maximum]
    return {
        "download_dir": str(root), "queue_initialized": True,
        "saved_jobs": len(saved_keys),
        "already_prepared": already_prepared, "ready": ready,
        "selected": selected, "deferred_by_limit": len(ready) - len(selected),
        "invalid": invalid,
        "chunk_policy": chunk_policy,
        **({"job_keys": sorted(scope), "scoped_out_jobs": sum(key not in scope for key in saved_keys)}
           if scope is not None else {}),
    }


def process_queue(download_dir: Path, bundle_root: Path, prepared_root: Path, *,
                  maximum: int = 1, execute: bool = False, tier: str = "standard",
                  timeout_minutes: int = 90, target_tokens: int = 600,
                  max_tokens: int = 900, tokenizer_json: Path | None = None,
                  job_keys=None, require_filed: bool = False,
                  bibliography_by_job: dict | None = None,
                  halt_on_failure: bool = False) -> dict:
    if type(halt_on_failure) is not bool:
        raise HandoffError("halt_on_failure must be boolean")
    queue = inspect_queue(download_dir, prepared_root, maximum=maximum, job_keys=job_keys,
                          require_filed=require_filed, target_tokens=target_tokens,
                          max_tokens=max_tokens, tokenizer_json=tokenizer_json,
                          bibliography_by_job=bibliography_by_job)
    results = []
    failures = list(queue["invalid"])
    if execute:
        for item in queue["selected"]:
            if halt_on_failure and failures:
                break
            try:
                results.append(handoff(
                    download_dir, item["job_key"], bundle_root, prepared_root,
                    execute=True, tier=tier, timeout_minutes=timeout_minutes,
                    target_tokens=target_tokens, max_tokens=max_tokens,
                    tokenizer_json=tokenizer_json,
                    bibliography=(bibliography_by_job or {}).get(item["job_key"]),
                    require_filed=require_filed,
                ))
            except (HandoffError, ImportError_, OSError, ValueError, RuntimeError) as exc:
                failures.append({"job_key": item["job_key"], "error": str(exc)})
    return {
        "mode": "execute" if execute else "dry-run", **queue,
        "processed": results, "failures": failures,
        "paused_requires_review": bool(execute and halt_on_failure and failures),
        "ragflow_write": False, "zotero_write": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download-dir", type=Path, required=True)
    parser.add_argument("--bundle-root", type=Path, default=default_path("bundle_root", "data/bundles"))
    parser.add_argument("--prepared-root", type=Path,
                        default=Path(__file__).resolve().parents[1] / "artifacts" / "prepared")
    parser.add_argument("--max", type=int, default=1, dest="maximum")
    parser.add_argument("--tier", choices=("flash", "basic", "standard", "advanced"), default="standard")
    parser.add_argument("--timeout-minutes", type=int, default=90)
    parser.add_argument("--target-tokens", type=int, default=600)
    parser.add_argument("--max-tokens", type=int, default=900)
    parser.add_argument("--tokenizer-json", type=Path,
                        help="explicit local tokenizer.json; omitted uses the heuristic estimate, not a model tokenizer")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = process_queue(
            args.download_dir, args.bundle_root, args.prepared_root,
            maximum=args.maximum, execute=args.execute, tier=args.tier,
            timeout_minutes=args.timeout_minutes, target_tokens=args.target_tokens,
            max_tokens=args.max_tokens, tokenizer_json=args.tokenizer_json,
        )
    except (HandoffError, ImportError_, OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
