#!/usr/bin/env python3
"""Run one bounded research-paper update cycle with separate write gates.

The default previews both SAVED-to-PREPARED and PREPARED-to-RAGFlow stages.
Local MinerU processing and RAGFlow writes have independent execution flags;
RAGFlow additionally requires explicit confirmation and an environment key.
Zotero is optional for compatibility; --with-zotero previews filing and gates
downstream jobs on verified FILED bindings. Its execution needs a separate
confirmation and interactive authorization. No institutional state is used.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from workflow_configuration import default_path
from handoff_saved_download import HandoffError
from import_prepared_queue import import_prepared_queue
from local_pdf_pipeline import build_chunk_policy
from prepare_chunks import PreparationError
from process_saved_queue import process_queue
from job_scope import validate_job_scope
from ragflow_prechunk_import import (
    DEFAULT_BASE,
    DEFAULT_STATE_DIR,
    ImportError_,
    RagflowApi,
)


def _stage_requires_review(stage, uncertain_field=None):
    if not isinstance(stage, dict) or not isinstance(stage.get("failures", []), list):
        raise HandoffError("Invalid update-stage failure result")
    return bool(stage.get("failures")) or bool(uncertain_field and stage.get(uncertain_field))


def _blocked_stage(reason):
    # No runner is invoked to produce this record: it is not a successful stage.
    return {"mode": "blocked", "reason_code": reason, "processed": [],
            "already_prepared": [], "failures": [], "ragflow_write": False,
            "ragflow_write_attempted": False, "ragflow_write_uncertain": False,
            "zotero_write": False}


def _abort_import(api):
    abort = getattr(type(api), "abort_import_session", None)
    if callable(abort):
        abort(api)  # In-memory closure only, never restore a failed authority.


def run_update_cycle(
    download_dir: Path,
    bundle_root: Path,
    prepared_root: Path,
    state_dir: Path,
    base_url: str,
    dataset_id: str,
    *,
    maximum: int = 1,
    execute_local: bool = False,
    execute_ragflow: bool = False,
    confirm_ragflow_write: bool = False,
    tier: str = "standard",
    timeout_minutes: int = 90,
    target_tokens: int = 600,
    max_tokens: int = 900,
    tokenizer_json: Path | None = None,
    api=None,
    local_runner=process_queue,
    import_runner=import_prepared_queue,
    with_zotero: bool = False,
    execute_zotero: bool = False,
    confirm_zotero_write: bool = False,
    zotero_client=None,
    zotero_lookup=None,
    zotero_metadata_by_job=None,
    zotero_storage_root: Path = default_path("zotero_storage", "data/zotero-storage"),
    filing_runner=None,
    job_keys=None,
    auto_cleanup: bool = True,
    with_materials: bool | None = None,
    material_runner=None,
    configuration: Path | None = None,
) -> dict:
    if configuration is not None:
        from workflow_configuration import read_configuration
        active = read_configuration(configuration)
    if with_materials is None:
        from workflow_configuration import DEFAULT_CONFIGURATION, read_configuration
        configuration_path = Path(configuration) if configuration is not None else DEFAULT_CONFIGURATION
        if configuration is None:
            active = read_configuration(configuration_path) if configuration_path.is_file() else {}
        with_materials = bool(with_zotero and job_keys is not None and active.get("scope") == "unified"
            and any(source.get("dataset_id") == dataset_id and Path(source.get("state_dir", "")).resolve() == Path(state_dir).resolve()
                    for source in active.get("sources", [])))
    if any(type(flag) is not bool for flag in (execute_local, execute_ragflow,
            confirm_ragflow_write, with_zotero, execute_zotero, confirm_zotero_write, auto_cleanup, with_materials)):
        raise HandoffError("Execution and confirmation flags must be boolean")
    if type(maximum) is not int or not 1 <= maximum <= 10:
        raise HandoffError("maximum must be an integer from 1 to 10")
    if execute_zotero and (not with_zotero or not confirm_zotero_write):
        raise HandoffError("execute_zotero requires with_zotero and explicit confirm_zotero_write")
    if execute_ragflow and not confirm_ragflow_write:
        raise HandoffError("execute_ragflow requires explicit confirm_ragflow_write")
    if execute_ragflow and api is None:
        raise HandoffError("execute_ragflow requires an initialized RAGFlow API client")
    if with_materials and (not with_zotero or job_keys is None):
        raise HandoffError("with_materials requires Zotero and an explicit job scope")
    requested_scope = validate_job_scope(job_keys)
    executing = execute_local or execute_zotero or execute_ragflow
    pause_reason = None
    try:
        chunk_policy = build_chunk_policy(
            target_tokens=target_tokens, max_tokens=max_tokens,
            tokenizer_json=tokenizer_json,
        )
    except PreparationError as exc:
        raise HandoffError(str(exc)) from exc

    filing = None
    local_options = {}
    import_options = {}
    if requested_scope is not None:
        local_options["job_keys"] = requested_scope
        import_options["job_keys"] = requested_scope
    if with_zotero:
        if filing_runner is None:
            paperdownload = str(Path(__file__).resolve().parents[2] / "Paperdownload")
            if paperdownload not in sys.path:
                sys.path.insert(0, paperdownload)
            from zotero_filing_queue import file_saved_queue
            filing_runner = file_saved_queue
        filing = filing_runner(download_dir, maximum=maximum,
                               execute=execute_zotero, confirm_zotero_write=confirm_zotero_write,
                               client=zotero_client, storage_root=zotero_storage_root,
                               metadata_by_job=zotero_metadata_by_job, job_keys=requested_scope,
                               **({"halt_on_failure": True} if execute_zotero else {}),
                               **({"candidate_lookup": zotero_lookup} if zotero_lookup is not None else {}))
        if not isinstance(filing, dict) or not isinstance(filing.get("eligible_job_keys"), list):
            raise HandoffError("Invalid Zotero filing-stage result")
        eligible = validate_job_scope(filing["eligible_job_keys"])
        selected = validate_job_scope(filing.get("selected_job_keys", []))
        if not eligible <= selected or len(selected) > maximum:
            raise HandoffError("Zotero filing scope exceeds the selected jobs")
        if requested_scope is not None and not selected <= requested_scope:
            raise HandoffError("Zotero filing scope exceeds the requested jobs")
        local_options["job_keys"] = eligible
        local_options["require_filed"] = True
        if executing and _stage_requires_review(filing, "zotero_write_uncertain"):
            pause_reason = "FILING_REQUIRES_REVIEW"
            _abort_import(api)
    if zotero_metadata_by_job is not None:
        metadata_scope = local_options.get("job_keys")
        if type(zotero_metadata_by_job) is not dict or metadata_scope is None:
            raise HandoffError("Supplementary bibliography requires an explicit matching job scope")
        if requested_scope is not None and not set(zotero_metadata_by_job) <= requested_scope:
            raise HandoffError("Supplementary bibliography exceeds the requested jobs")
        local_options["bibliography_by_job"] = {
            key: value for key, value in zotero_metadata_by_job.items() if key in metadata_scope
        }
    local = _blocked_stage(pause_reason) if pause_reason else local_runner(
        download_dir, bundle_root, prepared_root,
        maximum=maximum, execute=execute_local, tier=tier,
        timeout_minutes=timeout_minutes, target_tokens=target_tokens,
        max_tokens=max_tokens, tokenizer_json=tokenizer_json, **local_options,
        **({"halt_on_failure": True} if execute_local else {}),
    )
    # Import only artifacts the local stage confirmed under this request's
    # policy or successfully prepared during this cycle. In particular, a
    # read-only preview's `ready` rows have not been rechunked and cannot grant
    # permission to import their older PREPARED state.
    policy_prepared: set[str] = set()
    for item in local.get("already_prepared", []):
        if isinstance(item, dict) and isinstance(item.get("job_key"), str):
            policy_prepared.add(item["job_key"])
    for item in local.get("processed", []):
        verified = item.get("verified_job") if isinstance(item, dict) else None
        if isinstance(verified, dict) and isinstance(verified.get("job_key"), str):
            policy_prepared.add(verified["job_key"])
    failed_jobs = {
        item["job_key"] for item in local.get("failures", [])
        if isinstance(item, dict) and isinstance(item.get("job_key"), str)
    }
    policy_prepared.difference_update(failed_jobs)
    if requested_scope is not None:
        policy_prepared.intersection_update(requested_scope)
    if with_zotero:
        policy_prepared.intersection_update(eligible)
        import_options["require_filed"] = True
    import_options["job_keys"] = validate_job_scope(policy_prepared)
    if not pause_reason and executing and _stage_requires_review(local):
        pause_reason = "LOCAL_PREPARATION_REQUIRES_REVIEW"
        _abort_import(api)
    ragflow = _blocked_stage(pause_reason) if pause_reason else import_runner(
        download_dir, prepared_root, state_dir, base_url, dataset_id,
        maximum=maximum, execute=execute_ragflow,
        confirm_ragflow_write=confirm_ragflow_write, api=api, **import_options,
        **({"halt_on_failure": True} if execute_ragflow else {}),
    )
    if not pause_reason and executing and _stage_requires_review(ragflow, "ragflow_write_uncertain"):
        pause_reason = "INDEX_IMPORT_REQUIRES_REVIEW"
    material_results = []
    if with_materials and not pause_reason:
        from handoff_saved_download import verify_saved_job, HANDOFF_FILE, _load_object
        if material_runner is None:
            from bundle_materials import continue_materials
            material_runner = continue_materials
        handoffs = (_load_object(Path(download_dir) / HANDOFF_FILE, "processing state")
                    if (Path(download_dir) / HANDOFF_FILE).exists() else {"jobs": {}})
        for job_key in sorted(requested_scope.intersection(selected, eligible))[:maximum]:
            verified = verify_saved_job(download_dir, job_key)
            prepared = handoffs.get("jobs", {}).get(job_key, {})
            material_results.append({"job_key": job_key, **material_runner(
                Path(verified["pdf"]), prepared_root, bundle_root, state_dir, base_url, dataset_id,
                execute_local=execute_local, execute_ragflow=execute_ragflow, api=api,
                tokenizer_json=tokenizer_json, main_chunks_path=prepared.get("chunks_path"),
                **({"configuration": Path(configuration)} if configuration is not None else {}))})
            if material_results[-1].get("write_uncertain"):
                pause_reason = "MATERIAL_INDEX_REQUIRES_RECONCILIATION"
                break
    cleanup = []
    if auto_cleanup and not with_materials and with_zotero and execute_ragflow and not pause_reason:
        # Only the current bounded, FILED/PREPARED/indexed scope is considered.
        from paper_source_cleanup import cleanup_saved_job
        from zotero_local_api import ZoteroLocalClient
        cleanup_client = zotero_client if zotero_client is not None else ZoteroLocalClient()
        cleanup = [cleanup_saved_job(download_dir, key, client=cleanup_client,
            storage_root=zotero_storage_root, prepared_root=prepared_root,
            state_dir=state_dir, base_url=base_url, dataset_id=dataset_id,
            api=api, execute=True) for key in sorted(policy_prepared)[:maximum]]
        if any(row.get("status") == "RECOVERY_PENDING" for row in cleanup):
            pause_reason = "SOURCE_CLEANUP_RECOVERY_REQUIRED"
    return {
        "mode": "execute" if execute_local or execute_ragflow or execute_zotero else "dry-run",
        "download_dir": str(Path(download_dir).resolve()),
        "chunk_policy": chunk_policy,
        "policy_prepared_job_keys": sorted(policy_prepared),
        "paused_requires_review": pause_reason is not None,
        "pause_reason": pause_reason,
        "local_stage": local,
        "ragflow_stage": ragflow,
        "source_cleanup": cleanup,
        **({"material_stage": material_results} if with_materials else {}),
        "local_write": execute_local and local.get("mode") != "blocked",
        "ragflow_write": bool(ragflow.get("ragflow_write")),
        "ragflow_write_attempted": bool(ragflow.get(
            "ragflow_write_attempted", ragflow.get("ragflow_write"),
        )),
        "ragflow_write_uncertain": bool(ragflow.get("ragflow_write_uncertain")),
        "zotero_write": bool(filing and any(item.get("zotero_write_attempted")
                                             for item in filing.get("processed", []))),
        "failures": max(int(pause_reason is not None),
                    len(local.get("failures", [])) + len(ragflow.get("failures", []))
                    + (len(filing.get("failures", [])) if filing else 0)
                    + sum(len(item.get("failures", [])) for item in material_results)),
        **({"zotero_stage": filing,
            "zotero_write_attempted": bool(filing.get("zotero_write_attempted")),
            "zotero_write_uncertain": bool(filing.get("zotero_write_uncertain")),
            "awaiting_zotero_jobs": len(selected - eligible)} if filing is not None else {}),
    }


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--zotero-scope" in arguments:
        # An exact parent allowlist is independent of the SAVED job queue.
        # This branch never enters queue runners or reads online credentials.
        from zotero_scope_workflow import main as scope_main
        arguments[arguments.index("--zotero-scope")] = "--scope"
        return scope_main(arguments)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download-dir", type=Path, required=True)
    parser.add_argument("--bundle-root", type=Path, default=default_path("bundle_root", "data/bundles"))
    parser.add_argument("--prepared-root", type=Path,
                        default=Path(__file__).resolve().parents[1] / "artifacts" / "prepared")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--transport-base-url", help="Explicit HTTP API root; mapping identity stays --base-url")
    parser.add_argument("--configuration", type=Path,
                        help="Material target/source configuration; never fall back to personal targets when supplied")
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--max", type=int, default=1, dest="maximum")
    parser.add_argument("--tier", choices=("flash", "basic", "standard", "advanced"),
                        default="standard")
    parser.add_argument("--timeout-minutes", type=int, default=90)
    parser.add_argument("--target-tokens", type=int, default=600)
    parser.add_argument("--max-tokens", type=int, default=900)
    parser.add_argument("--tokenizer-json", type=Path,
                        help="explicit local tokenizer.json; omitted uses the heuristic estimate, not a model tokenizer")
    parser.add_argument("--execute-local", action="store_true")
    parser.add_argument("--execute-ragflow", action="store_true")
    parser.add_argument("--confirm-ragflow-write", action="store_true")
    parser.add_argument("--with-zotero", action="store_true", help="preview filing and gate downstream on FILED")
    parser.add_argument("--with-materials", action="store_true", default=None, help="continue bundle materials independently; retain originals (automatic for the published unified target)")
    parser.add_argument("--execute-zotero", action="store_true")
    parser.add_argument("--confirm-zotero-write", action="store_true")
    parser.add_argument("--job-key", action="append", help="optional exact job allowlist; repeat for multiple")
    parser.add_argument("--zotero-metadata", type=Path, help="public bibliographic JSON for exactly one --job-key")
    args = parser.parse_args(argv)
    try:
        # Validate an explicitly selected tokenizer before reading credentials,
        # parsing filing metadata, or entering any downstream stage.
        try:
            build_chunk_policy(
                target_tokens=args.target_tokens, max_tokens=args.max_tokens,
                tokenizer_json=args.tokenizer_json,
            )
        except PreparationError as exc:
            raise HandoffError(str(exc)) from None
        if args.execute_zotero and (not args.with_zotero or not args.confirm_zotero_write):
            raise HandoffError("--execute-zotero requires --with-zotero and --confirm-zotero-write")
        validate_job_scope(args.job_key)
        metadata_by_job = None
        if args.zotero_metadata is not None:
            if not args.with_zotero or args.job_key is None or len(args.job_key) != 1:
                raise HandoffError("--zotero-metadata requires --with-zotero and exactly one --job-key")
            raw_metadata = args.zotero_metadata.read_bytes()
            if len(raw_metadata) > 64 * 1024:
                raise HandoffError("Public bibliographic JSON exceeds 64 KiB")
            metadata = json.loads(raw_metadata.decode("utf-8"))
            if not isinstance(metadata, dict):
                raise HandoffError("Public bibliographic JSON must be an object")
            public_fields = {"title", "authors", "published", "journal", "doi", "pmcid"}
            if not set(metadata) <= public_fields | {"metadata_source"}:
                raise HandoffError("Public bibliographic JSON contains unsupported fields")
            metadata_by_job = {args.job_key[0]: {field: value for field, value in metadata.items()
                                               if field in public_fields}}
        api = None
        key = ""
        if args.execute_ragflow:
            if not args.confirm_ragflow_write:
                raise HandoffError("--execute-ragflow requires --confirm-ragflow-write")
            key = os.environ.get("RAGFLOW_API_KEY", "")
            if not key:
                raise HandoffError("--execute-ragflow requires RAGFLOW_API_KEY in the environment")
            api = RagflowApi(args.base_url, key, transport_base_url=args.transport_base_url)
        try:
            result = run_update_cycle(
                args.download_dir, args.bundle_root, args.prepared_root,
                args.state_dir, args.base_url, args.dataset_id,
                maximum=args.maximum, execute_local=args.execute_local,
                execute_ragflow=args.execute_ragflow,
                confirm_ragflow_write=args.confirm_ragflow_write,
                tier=args.tier, timeout_minutes=args.timeout_minutes,
                target_tokens=args.target_tokens, max_tokens=args.max_tokens,
                tokenizer_json=args.tokenizer_json,
                api=api,
                with_zotero=args.with_zotero, execute_zotero=args.execute_zotero,
                confirm_zotero_write=args.confirm_zotero_write,
                zotero_metadata_by_job=metadata_by_job, job_keys=args.job_key,
                with_materials=args.with_materials, configuration=args.configuration,
            )
        finally:
            if api is not None:
                api.api_key = ""
            key = ""
    except (HandoffError, ImportError_, OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
