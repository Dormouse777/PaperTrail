#!/usr/bin/env python3
"""Run one explicit local PDF through MinerU conversion and chunk preparation.

The command is local-only and stops before any Zotero or RAGFlow write. Preview
is the default. With --execute it reuses a verified existing MinerU bundle or
creates one through the loopback V1 API, then writes content-addressed chunk
artifacts. Each stage remains independently idempotent. Without --tokenizer-json,
chunking uses the documented heuristic estimate, not a model tokenizer.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from workflow_configuration import default_path
from mineru_v1_convert import ConversionError, convert
from prepare_chunks import PreparationError, prepare, validate_zotero_provenance, validate_paper_provenance
from ragflow_prechunk_import import ImportError_
from structure_aware_chunker import load_local_token_counter


DEFAULT_BUNDLE_ROOT = default_path("bundle_root", "data/bundles")
DEFAULT_PREPARED_ROOT = default_path("prepared_root", "data/prepared")


def build_chunk_policy(*, target_tokens: int = 600, max_tokens: int = 900,
                       include_references: bool = False,
                       include_front_matter: bool = False,
                       tokenizer_json: Path | None = None) -> dict:
    if (type(target_tokens) is not int or type(max_tokens) is not int
            or target_tokens < 1 or max_tokens < target_tokens):
        raise PreparationError("Expected 1 <= target_tokens <= max_tokens")
    if type(include_references) is not bool or type(include_front_matter) is not bool:
        raise PreparationError("Chunk inclusion flags must be boolean")
    policy = {
        "target_tokens": target_tokens,
        "max_tokens": max_tokens,
        "include_references": include_references,
        "include_front_matter": include_front_matter,
    }
    if tokenizer_json is not None:
        try:
            counter = load_local_token_counter(Path(tokenizer_json))
            metadata = getattr(counter, "metadata", None)
            if not isinstance(metadata, dict):
                raise ValueError
            # Keep the same metadata (including special-token and template flags)
            # that prepare_chunks writes into manifest.chunk_policy.
            json.dumps(metadata, sort_keys=True, allow_nan=False)
        except Exception:
            raise PreparationError(
                "Cannot load local tokenizer_json; expected a valid local tokenizer JSON"
            ) from None
        policy["tokenizer"] = dict(metadata)
    return policy


def _verify_prepared_policy(preparation: dict, expected_policy: dict,
                            paper_provenance: dict | None = None) -> None:
    target_dir = preparation.get("target_dir")
    chunks_path = preparation.get("chunks_path")
    if not isinstance(target_dir, str) or not Path(target_dir).is_absolute():
        raise PreparationError("Prepared result has no absolute manifest directory")
    if not isinstance(chunks_path, str) or not Path(chunks_path).is_absolute():
        raise PreparationError("Prepared result has no absolute chunks path")
    target = Path(target_dir)
    chunks = Path(chunks_path)
    manifest_path = target / "manifest.json"
    if (target.is_symlink() or chunks.is_symlink() or manifest_path.is_symlink()
            or chunks.parent.resolve() != target.resolve()
            or not manifest_path.is_file()):
        raise PreparationError("Prepared manifest is missing or outside its artifact directory")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise PreparationError("Prepared manifest is unreadable or invalid") from None
    if (not isinstance(manifest, dict) or manifest.get("status") != "PREPARED"
            or manifest.get("chunk_policy") != expected_policy):
        raise PreparationError("Prepared manifest chunk_policy changed during preparation")
    if paper_provenance is not None and manifest.get("paper_provenance") != paper_provenance:
        raise PreparationError("Prepared public paper provenance changed during preparation")


def run_pipeline(pdf: Path, bundle_root: Path = DEFAULT_BUNDLE_ROOT,
                 prepared_root: Path = DEFAULT_PREPARED_ROOT, *,
                 tier: str = "standard", timeout_minutes: int = 90,
                 target_tokens: int = 600, max_tokens: int = 900,
                 include_references: bool = False,
                 include_front_matter: bool = False,
                 tokenizer_json: Path | None = None,
                 zotero_provenance: dict[str, str] | None = None,
                 paper_provenance: dict | None = None,
                 execute: bool = False) -> dict:
    zotero_provenance = validate_zotero_provenance(zotero_provenance)
    paper_provenance = validate_paper_provenance(paper_provenance)
    chunk_policy = build_chunk_policy(
        target_tokens=target_tokens, max_tokens=max_tokens,
        include_references=include_references,
        include_front_matter=include_front_matter,
        tokenizer_json=tokenizer_json,
    )
    conversion = convert(pdf, bundle_root, tier=tier,
                         timeout_minutes=timeout_minutes, execute=execute)
    result = {
        "mode": "execute" if execute else "dry-run",
        "source_pdf": conversion["source_pdf"],
        "source_pdf_sha256": conversion["source_pdf_sha256"],
        "conversion": conversion,
        "preparation": None,
        "ragflow_write": False,
        "zotero_write": False,
        "chunk_policy": chunk_policy,
    }
    if paper_provenance is not None:
        if conversion["source_pdf_sha256"].lower() != paper_provenance["source_pdf_sha256"]:
            raise PreparationError("Public paper provenance conflicts with the converted PDF")
        result["paper_provenance"] = paper_provenance
    if conversion["mode"] == "dry-run":
        result["next_action"] = "Run with --execute to create the MinerU bundle and prepared chunks"
        return result

    bundle = Path(conversion["target_dir"])
    preparation = prepare(
        bundle, prepared_root, target_tokens=target_tokens, max_tokens=max_tokens,
        include_references=include_references,
        include_front_matter=include_front_matter, execute=execute,
        tokenizer_json=tokenizer_json,
        zotero_provenance=zotero_provenance,
        paper_provenance=paper_provenance,
    )
    if preparation["source_pdf_sha256"].lower() != conversion["source_pdf_sha256"].lower():
        raise PreparationError("Conversion and preparation source hashes disagree")
    _verify_prepared_policy(preparation, chunk_policy, paper_provenance)
    result["preparation"] = preparation
    result["next_action"] = "Use the chunks_path with the RAGFlow importer in a separately authorized step"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, help="one explicit local PDF; no directory discovery")
    parser.add_argument("--bundle-root", type=Path, default=DEFAULT_BUNDLE_ROOT)
    parser.add_argument("--prepared-root", type=Path, default=DEFAULT_PREPARED_ROOT)
    parser.add_argument("--tier", choices=("flash", "basic", "standard", "advanced"), default="standard")
    parser.add_argument("--timeout-minutes", type=int, default=90)
    parser.add_argument("--target-tokens", type=int, default=600)
    parser.add_argument("--max-tokens", type=int, default=900)
    parser.add_argument("--include-references", action="store_true")
    parser.add_argument("--include-front-matter", action="store_true")
    parser.add_argument("--tokenizer-json", type=Path,
                        help="explicit local tokenizer.json; omitted uses the heuristic estimate, not a model tokenizer")
    parser.add_argument("--execute", action="store_true",
                        help="create/reuse the local MinerU bundle and prepared chunk artifacts")
    args = parser.parse_args(argv)
    try:
        result = run_pipeline(
            args.pdf, args.bundle_root, args.prepared_root,
            tier=args.tier, timeout_minutes=args.timeout_minutes,
            target_tokens=args.target_tokens, max_tokens=args.max_tokens,
            include_references=args.include_references,
            include_front_matter=args.include_front_matter,
            tokenizer_json=args.tokenizer_json, execute=args.execute,
        )
    except (ConversionError, PreparationError, ImportError_, OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
