#!/usr/bin/env python3
"""Hand one verified SAVED downloader job to the local PDF preparation pipeline.

The caller must name both the download directory and exact job key. The script
cross-checks the job record, local PDF, and download ledger before doing
anything. Preview is the default. --execute may call only the local MinerU
pipeline and records a small recoverable handoff state; it never writes Zotero
or RAGFlow.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from workflow_configuration import default_path
from local_pdf_pipeline import run_pipeline
from mineru_v1_convert import ConversionError
from prepare_chunks import PreparationError
from ragflow_prechunk_import import ImportError_


JOBS_FILE = ".paper-download-jobs.json"
LEDGER_FILE = ".paper-download-ledger.json"
HANDOFF_FILE = ".paper-processing-state.json"
ZOTERO_FILING_FILE = ".paper-zotero-filing.json"
HEX_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
ZOTERO_KEY = re.compile(r"[A-Z0-9]{8}\Z")
HANDOFF_STATE_MAX_BYTES = 4 * 1024 * 1024
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class HandoffError(RuntimeError):
    pass


def _is_reparse_point(info) -> bool:
    return (stat.S_ISLNK(info.st_mode) or
            bool(getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT))


def _file_identity(info) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _processing_state_file_info(path: Path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise HandoffError("Cannot inspect processing state file") from exc
    if _is_reparse_point(info):
        raise HandoffError("Processing state must not be a symlink or reparse point")
    if not stat.S_ISREG(info.st_mode):
        raise HandoffError("Processing state is not a regular file")
    if info.st_size > HANDOFF_STATE_MAX_BYTES:
        raise HandoffError("Processing state exceeds the size limit")
    return info


def _read_processing_state(path: Path) -> bytes:
    before = _processing_state_file_info(path)
    if before is None:
        raise HandoffError("Missing regular processing state file")
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0) |
             getattr(os, "O_NOFOLLOW", 0))
    descriptor = os.open(os.fspath(path), flags)
    try:
        opened = os.fstat(descriptor)
        if (_is_reparse_point(opened) or not stat.S_ISREG(opened.st_mode) or
                _file_identity(opened) != _file_identity(before) or
                opened.st_size > HANDOFF_STATE_MAX_BYTES):
            raise HandoffError("Processing state changed while opening")
        data = bytearray()
        while len(data) <= HANDOFF_STATE_MAX_BYTES:
            remaining = HANDOFF_STATE_MAX_BYTES + 1 - len(data)
            block = os.read(descriptor, min(64 * 1024, remaining))
            if not block:
                break
            data.extend(block)
        if len(data) > HANDOFF_STATE_MAX_BYTES:
            raise HandoffError("Processing state exceeds the size limit")
        after = _processing_state_file_info(path)
        if after is None or _file_identity(after) != _file_identity(before):
            raise HandoffError("Processing state changed while reading")
        return bytes(data)
    finally:
        os.close(descriptor)


def _processing_directory_chain(path: Path):
    raw_path = Path(path)
    if ".." in raw_path.parts:
        raise HandoffError("Processing state parent path must not contain '..'")
    try:
        absolute = Path(os.path.abspath(os.fspath(raw_path)))
    except (OSError, TypeError, ValueError) as exc:
        raise HandoffError("Download directory must be a regular directory") from exc
    chain = []
    for component in reversed((absolute, *absolute.parents)):
        try:
            info = component.lstat()
        except OSError as exc:
            raise HandoffError("Cannot inspect processing state parent directory") from exc
        if _is_reparse_point(info) or not stat.S_ISDIR(info.st_mode):
            raise HandoffError("Processing state parent must be a regular directory")
        chain.append((component, _file_identity(info)))
    return absolute, tuple(chain)


def _verify_processing_directory_chain(chain) -> None:
    for component, expected_identity in chain:
        try:
            info = component.lstat()
        except OSError as exc:
            raise HandoffError("Processing state parent directory changed") from exc
        if (_is_reparse_point(info) or not stat.S_ISDIR(info.st_mode) or
                _file_identity(info) != expected_identity):
            raise HandoffError("Processing state parent directory changed")


def _verify_processing_target(path: Path, expected_identity) -> None:
    info = _processing_state_file_info(path)
    identity = None if info is None else _file_identity(info)
    if identity != expected_identity:
        raise HandoffError("Processing state target changed")


def _cleanup_owned_temporary(path: Path, identity, parent_chain) -> None:
    if identity is None:
        return
    try:
        _verify_processing_directory_chain(parent_chain)
        info = path.lstat()
        if (_is_reparse_point(info) or not stat.S_ISREG(info.st_mode) or
                _file_identity(info) != identity):
            return
        path.unlink()
    except (HandoffError, OSError):
        # A path we can no longer prove we created must be left untouched.
        return


def _load_object(path: Path, label: str) -> dict:
    if label == "processing state":
        try:
            value = json.loads(_read_processing_state(path).decode("utf-8"))
        except HandoffError:
            raise
        except (OSError, UnicodeError, ValueError) as exc:
            raise HandoffError("Invalid processing state JSON") from exc
    else:
        if not path.is_file() or path.is_symlink():
            raise HandoffError(f"Missing regular {label} file")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HandoffError(f"Invalid {label} JSON") from exc
    if not isinstance(value, dict) or value.get("version") != 1:
        raise HandoffError(f"Unsupported {label} format")
    return value


def verify_saved_job(download_dir: Path, job_key: str) -> dict:
    root = Path(download_dir).resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise HandoffError("Download directory must be a regular directory")
    jobs = _load_object(root / JOBS_FILE, "download jobs")
    ledger = _load_object(root / LEDGER_FILE, "download ledger")
    job = jobs.get("jobs", {}).get(job_key)
    if not isinstance(job, dict):
        raise HandoffError("Requested job key was not found")
    if job.get("status") != "SAVED":
        raise HandoffError("Requested job is not in SAVED state")
    filename = job.get("pdf_filename")
    expected_hash = job.get("pdf_sha256")
    expected_size = job.get("pdf_size_bytes")
    if not isinstance(filename, str) or Path(filename).name != filename or not filename.lower().endswith(".pdf"):
        raise HandoffError("Saved job has an unsafe PDF filename")
    if not isinstance(expected_hash, str) or not HEX_SHA256.fullmatch(expected_hash):
        raise HandoffError("Saved job has no valid PDF SHA-256")
    if type(expected_size) is not int or expected_size < 1024:
        raise HandoffError("Saved job has no valid PDF size")
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Paperdownload"))
    from download_ledger import DownloadLedger, LedgerConflict
    rows = [row for row in ledger.get("entries", {}).values() if isinstance(row, dict)
            and row.get("filename") == filename and row.get("sha256") == expected_hash]
    if not rows or any(row != rows[0] for row in rows):
        raise HandoffError("Download ledger conflicts with the saved job or lacks an unambiguous PDF entry")
    try:
        ledger_reader = DownloadLedger(root)
        pdf = ledger_reader.source_path(rows[0])
        if not pdf.exists():
            pdf, _, _ = ledger_reader._verify_entry(rows[0])
    except (LedgerConflict, OSError):
        raise HandoffError("Saved PDF and its verified recovery locations are unavailable") from None
    digest = hashlib.sha256()
    with pdf.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise HandoffError("Saved PDF header is invalid")
        stream.seek(0)
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    actual_hash = digest.hexdigest()
    actual_size = pdf.stat().st_size
    if actual_hash.lower() != expected_hash.lower() or actual_size != expected_size:
        raise HandoffError("Saved PDF no longer matches its job record")

    identifiers = job.get("identifiers")
    if not isinstance(identifiers, list) or not identifiers:
        raise HandoffError("Saved job has no publication identifiers")
    entries = ledger.get("entries", {})
    matched = 0
    for identifier in identifiers:
        entry = entries.get(identifier)
        if entry is None:
            continue
        if not isinstance(entry, dict) or entry.get("filename") != filename or \
                str(entry.get("sha256", "")).lower() != actual_hash.lower() or \
                entry.get("size_bytes") != actual_size:
            raise HandoffError("Download ledger conflicts with the saved job")
        matched += 1
    if not matched:
        raise HandoffError("No download ledger entry confirms the saved PDF")
    return {
        "job_key": job_key, "source": job.get("source"),
        "source_id": job.get("source_id"), "title": job.get("title"),
        "identifiers": identifiers, "pdf": str(pdf),
        "pdf_sha256": actual_hash, "pdf_size_bytes": actual_size,
        "ledger_matches": matched,
    }


def read_filed_zotero_provenance(download_dir: Path, job_key: str,
                                 source_pdf_sha256: str) -> dict[str, str] | None:
    """Return only the four approved keys from a verified FILED sidecar entry."""
    root = Path(download_dir).resolve(strict=True)
    sidecar = root / ZOTERO_FILING_FILE
    try:
        sidecar_stat = sidecar.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise HandoffError("Cannot inspect Zotero filing sidecar") from exc
    if stat.S_ISLNK(sidecar_stat.st_mode):
        raise HandoffError("Zotero filing sidecar must not be a symlink")
    if not stat.S_ISREG(sidecar_stat.st_mode):
        raise HandoffError("Zotero filing sidecar is not a regular file")
    try:
        raw = sidecar.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, ValueError) as exc:
        raise HandoffError("Cannot read Zotero filing sidecar") from exc
    try:
        payload = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise HandoffError("Invalid Zotero filing sidecar JSON") from exc
    if (not isinstance(payload, dict) or set(payload) != {"version", "server_id", "jobs"}
            or type(payload.get("version")) is not int or payload["version"] != 1):
        raise HandoffError("Unsupported Zotero filing sidecar format")
    server_id = payload.get("server_id")
    jobs = payload.get("jobs")
    if not isinstance(server_id, str) or not server_id.strip() or not isinstance(jobs, dict):
        raise HandoffError("Zotero filing sidecar has invalid server_id or jobs")
    if job_key not in jobs:
        return None
    binding = jobs[job_key]
    if not isinstance(binding, dict):
        raise HandoffError("Zotero filing entry must be an object")
    status = binding.get("status")
    if isinstance(status, str) and status in {"PARTIAL", "FAILED"}:
        return None
    if status != "FILED":
        raise HandoffError("Zotero filing entry has an unsupported status")
    required = {"status", "source_pdf_sha256", "parent_key", "pdf_attachment_key",
                "collection_key", "server_id"}
    optional_audit = {"zotero_write_attempted", "zotero_write_uncertain", "updated_at",
                      "topic_keyword", "collection_path"}
    binding_fields = set(binding)
    if not required <= binding_fields or not binding_fields <= (required | optional_audit):
        raise HandoffError("FILED Zotero binding has unexpected or missing fields")
    if ("zotero_write_attempted" in binding and
            type(binding["zotero_write_attempted"]) is not bool):
        raise HandoffError("FILED Zotero binding has an invalid write-attempt audit field")
    if "zotero_write_uncertain" in binding:
        if type(binding["zotero_write_uncertain"]) is not bool:
            raise HandoffError("FILED Zotero binding has an invalid write-uncertain audit field")
        if binding["zotero_write_uncertain"]:
            raise HandoffError("FILED Zotero binding has an uncertain Zotero write")
    if "updated_at" in binding:
        timestamp = binding["updated_at"]
        if (not isinstance(timestamp, str) or not timestamp or timestamp != timestamp.strip()
                or ("T" not in timestamp and "t" not in timestamp)):
            raise HandoffError("FILED Zotero binding has an invalid updated_at audit field")
        try:
            datetime.fromisoformat(timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp)
        except ValueError as exc:
            raise HandoffError("FILED Zotero binding has an invalid updated_at audit field") from exc
    binding_hash = binding.get("source_pdf_sha256")
    if not isinstance(binding_hash, str) or not HEX_SHA256.fullmatch(binding_hash):
        raise HandoffError("FILED Zotero binding has an invalid source PDF hash")
    if (not isinstance(source_pdf_sha256, str) or not HEX_SHA256.fullmatch(source_pdf_sha256)
            or binding_hash.lower() != source_pdf_sha256.lower()):
        raise HandoffError("FILED Zotero binding does not match the verified local PDF")
    if binding.get("server_id") != server_id:
        raise HandoffError("FILED Zotero binding server_id conflicts with the sidecar")
    keys = (binding.get("parent_key"), binding.get("pdf_attachment_key"),
            binding.get("collection_key"))
    if any(not isinstance(value, str) or not ZOTERO_KEY.fullmatch(value) for value in keys):
        raise HandoffError("FILED Zotero binding has an invalid library key")
    # Exact field validation above also prevents credential fields from crossing
    # this boundary; only this fixed allowlist is returned to preparation.
    return {
        "zotero_parent_key": binding["parent_key"],
        "zotero_pdf_attachment_key": binding["pdf_attachment_key"],
        "zotero_collection_key": binding["collection_key"],
        "zotero_server_id": binding["server_id"],
    }


def _record_handoff(download_dir: Path, verified: dict, pipeline: dict,
                    zotero_provenance: dict[str, str] | None) -> Path:
    preparation = pipeline.get("preparation")
    if not isinstance(preparation, dict) or preparation.get("mode") not in {"prepared", "exists"}:
        raise HandoffError("Pipeline did not produce verified prepared chunks")
    root, parent_chain = _processing_directory_chain(Path(download_dir))
    state_path = root / HANDOFF_FILE
    previous_identity_info = _processing_state_file_info(state_path)
    previous_identity = (None if previous_identity_info is None else
                         _file_identity(previous_identity_info))
    if previous_identity is not None:
        state = _load_object(state_path, "processing state")
        _verify_processing_directory_chain(parent_chain)
        _verify_processing_target(state_path, previous_identity)
    else:
        state = {"version": 1, "jobs": {}}
    jobs = state.setdefault("jobs", {})
    if not isinstance(jobs, dict):
        raise HandoffError("Processing state jobs must be an object")
    record = {
        "status": "PREPARED", "source_pdf_sha256": verified["pdf_sha256"],
        "bundle_dir": pipeline["conversion"]["target_dir"],
        "chunks_path": preparation["chunks_path"],
        "fingerprint": preparation["fingerprint"], "chunk_count": preparation["chunks"],
        "ragflow_write": False, "zotero_write": False,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if isinstance(pipeline.get("chunk_policy"), dict):
        record["chunk_policy"] = pipeline["chunk_policy"]
    if isinstance(pipeline.get("paper_provenance"), dict):
        record["paper_provenance"] = pipeline["paper_provenance"]
    if zotero_provenance is not None:
        record["zotero_provenance"] = zotero_provenance
    previous = jobs.get(verified["job_key"])
    if isinstance(previous, dict) and previous.get("source_pdf_sha256") not in {None, verified["pdf_sha256"]}:
        raise HandoffError("Existing processing state belongs to different PDF content")
    jobs[verified["job_key"]] = record
    temporary = state_path.with_name(f"{state_path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = None
    created = False
    temporary_identity = None
    try:
        data = (json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False) +
                "\n").encode("utf-8")
        if len(data) > HANDOFF_STATE_MAX_BYTES:
            raise ValueError("processing state exceeds the size limit")
        _verify_processing_directory_chain(parent_chain)
        _verify_processing_target(state_path, previous_identity)
        flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                 getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        descriptor = os.open(os.fspath(temporary), flags, 0o600)
        created = True
        opened = os.fstat(descriptor)
        temporary_identity = _file_identity(opened)
        if _is_reparse_point(opened) or not stat.S_ISREG(opened.st_mode):
            raise OSError("temporary file is not regular")
        _verify_processing_directory_chain(parent_chain)
        stream = os.fdopen(descriptor, "wb")
        descriptor = None
        with stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _verify_processing_directory_chain(parent_chain)
        _verify_processing_target(state_path, previous_identity)
        temporary_info = temporary.lstat()
        if (_is_reparse_point(temporary_info) or
                not stat.S_ISREG(temporary_info.st_mode) or
                _file_identity(temporary_info) != temporary_identity):
            raise OSError("temporary file identity changed")
        os.replace(temporary, state_path)
    except Exception:
        raise HandoffError("HANDOFF_STATE_WRITE_FAILED") from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if created:
            _cleanup_owned_temporary(temporary, temporary_identity, parent_chain)
    return state_path


def handoff(download_dir: Path, job_key: str, bundle_root: Path, prepared_root: Path, *,
            execute: bool = False, tier: str = "standard", timeout_minutes: int = 90,
            target_tokens: int = 600, max_tokens: int = 900,
            tokenizer_json: Path | None = None,
            bibliography: dict | None = None,
            require_filed: bool = False) -> dict:
    if type(require_filed) is not bool:
        raise HandoffError("require_filed must be boolean")
    verified = verify_saved_job(download_dir, job_key)
    zotero_provenance = read_filed_zotero_provenance(
        download_dir, job_key, verified["pdf_sha256"])
    if require_filed and zotero_provenance is None:
        raise HandoffError("A verified FILED binding is required before local preparation")
    # Lazy import avoids a module cycle: the projection reuses verify_saved_job.
    paper_provenance = None
    if verified.get("source") == "pubmed":
        from paper_provenance_projection import project_saved_paper_provenance, ProvenanceProjectionError
        try:
            paper_provenance = project_saved_paper_provenance(
                download_dir, job_key, bibliography=bibliography)
        except ProvenanceProjectionError as exc:
            raise HandoffError(str(exc)) from None
        if paper_provenance["source_pdf_sha256"] != verified["pdf_sha256"]:
            raise HandoffError("Public paper provenance changed after verification")
    elif bibliography is not None:
        raise HandoffError("Supplementary bibliography requires a verified PubMed job")
    pipeline = run_pipeline(
        Path(verified["pdf"]), bundle_root, prepared_root,
        execute=execute, tier=tier, timeout_minutes=timeout_minutes,
        target_tokens=target_tokens, max_tokens=max_tokens,
        tokenizer_json=tokenizer_json,
        zotero_provenance=zotero_provenance,
        paper_provenance=paper_provenance,
    )
    result = {"mode": "execute" if execute else "dry-run", "verified_job": verified,
              "pipeline": pipeline, "processing_state": None}
    if zotero_provenance is not None:
        result["zotero_provenance"] = zotero_provenance
    if execute:
        result["processing_state"] = str(_record_handoff(
            download_dir, verified, pipeline, zotero_provenance))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download-dir", type=Path, required=True)
    parser.add_argument("--job-key", required=True, help="exact key such as pubmed:34098886")
    parser.add_argument("--bundle-root", type=Path, default=default_path("bundle_root", "data/bundles"))
    parser.add_argument("--prepared-root", type=Path,
                        default=Path(__file__).resolve().parents[1] / "artifacts" / "prepared")
    parser.add_argument("--tier", choices=("flash", "basic", "standard", "advanced"), default="standard")
    parser.add_argument("--timeout-minutes", type=int, default=90)
    parser.add_argument("--target-tokens", type=int, default=600)
    parser.add_argument("--max-tokens", type=int, default=900)
    parser.add_argument("--tokenizer-json", type=Path,
                        help="explicit local tokenizer.json; omitted uses the heuristic estimate, not a model tokenizer")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = handoff(
            args.download_dir, args.job_key, args.bundle_root, args.prepared_root,
            execute=args.execute, tier=args.tier, timeout_minutes=args.timeout_minutes,
            target_tokens=args.target_tokens, max_tokens=args.max_tokens,
            tokenizer_json=args.tokenizer_json,
        )
    except (HandoffError, ConversionError, PreparationError, ImportError_,
            OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
