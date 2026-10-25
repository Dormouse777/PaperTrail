#!/usr/bin/env python3
"""Prepare a verified MinerU bundle for the existing RAGFlow prechunk importer.

The PDF and MinerU bundle are read-only. Derived artifacts are content-addressed,
so a changed parse or changed chunk policy creates a new version rather than
silently overwriting a previous one. No RAGFlow request is made here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from pathlib import Path

from mineru_adapter import adapt_bundle
from ragflow_prechunk_import import DOCUMENT_ID_PATTERN, ImportError_, load_bundle
from structure_aware_chunker import chunk_document, estimate_tokens, load_local_token_counter
from windows_path_io import is_file, open_path, read_bytes, stat_path


DEFAULT_OUTPUT_ROOT = Path(__file__).resolve().parents[1] / "artifacts" / "prepared"
HEX_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
ZOTERO_KEY = re.compile(r"[A-Z0-9]{8}\Z")
ZOTERO_PROVENANCE_KEYS = {
    "zotero_parent_key", "zotero_pdf_attachment_key",
    "zotero_collection_key", "zotero_server_id",
}
PAPER_PROVENANCE_KEYS = {
    "source_database", "source_id", "source_pdf_sha256", "doi",
    "publication_date", "publication_date_source",
}


class PreparationError(RuntimeError):
    pass


def validate_paper_provenance(value: dict | None) -> dict | None:
    """Validate the public projection; actual PDF hash binding is checked below."""
    if value is None:
        return None
    required = {"source_database", "source_id", "source_pdf_sha256"}
    if (type(value) is not dict or not required <= set(value)
            or not set(value) <= PAPER_PROVENANCE_KEYS):
        raise PreparationError("Invalid public paper provenance fields")
    if (value.get("source_database") != "PubMed"
            or not isinstance(value.get("source_id"), str)
            or not re.fullmatch(r"[0-9]{1,16}", value["source_id"])
            or not isinstance(value.get("source_pdf_sha256"), str)
            or not HEX_SHA256.fullmatch(value["source_pdf_sha256"])):
        raise PreparationError("Invalid public paper provenance identity")
    for field, limit in (("doi", 255), ("publication_date", 64), ("publication_date_source", 256)):
        item = value.get(field)
        if item is not None and (not isinstance(item, str) or not item.strip()
                or len(item) > limit or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in item)):
            raise PreparationError("Invalid public paper provenance text")
    doi = value.get("doi")
    if doi is not None and (doi != doi.lower() or not re.fullmatch(r"10\.[0-9]{4,9}/\S+", doi)
            or "?" in doi or "#" in doi):
        raise PreparationError("Invalid public paper provenance DOI")
    date_source = value.get("publication_date_source")
    if date_source is not None and (not value.get("publication_date")
            or re.search(r"[/?#@\\]|://", date_source)
            or re.search(r"(?i)\b(token|session|auth|cookie|password|secret|api[_-]?key|query)\b", date_source)
            or re.search(r"\b(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b", date_source)):
        raise PreparationError("Invalid public paper provenance date source")
    return {key: (value[key].lower() if key == "source_pdf_sha256" else value[key])
            for key in sorted(value)}


def validate_zotero_provenance(value: dict[str, str] | None) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != ZOTERO_PROVENANCE_KEYS:
        raise PreparationError("zotero_provenance must contain only the four approved fields")
    for field in ZOTERO_PROVENANCE_KEYS - {"zotero_server_id"}:
        item = value.get(field)
        if not isinstance(item, str) or not ZOTERO_KEY.fullmatch(item):
            raise PreparationError(f"Invalid {field} in zotero_provenance")
    server_id = value.get("zotero_server_id")
    if not isinstance(server_id, str) or not server_id.strip():
        raise PreparationError("Invalid zotero_server_id in zotero_provenance")
    return {field: value[field] for field in sorted(ZOTERO_PROVENANCE_KEYS)}


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _verify_source_pdf(provenance: dict, bundle: Path | None = None) -> tuple[Path, str]:
    raw_path = provenance.get("sourcePdf")
    expected = provenance.get("sourcePdfSha256")
    candidate = Path(raw_path) if isinstance(raw_path, str) else None
    if (candidate is None or not candidate.is_absolute() or not is_file(candidate)) and provenance.get("source_provenance_inferred") and bundle:
        local_pdfs = sorted(bundle.glob("*.pdf"))
        candidate = local_pdfs[0] if local_pdfs else None
    if candidate is None or not candidate.is_absolute():
        raise PreparationError("MinerU provenance has no absolute sourcePdf path")
    if not isinstance(expected, str) or not HEX_SHA256.fullmatch(expected):
        raise PreparationError("MinerU provenance has no valid sourcePdfSha256")
    path = candidate
    try:
        source_stat = stat_path(path)
    except OSError as exc:
        raise PreparationError("Source PDF is missing or inaccessible") from exc
    if source_stat.st_size < 1024:
        raise PreparationError("Source PDF is missing or smaller than 1 KiB")
    digest = hashlib.sha256()
    with open_path(path, "rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise PreparationError("Source PDF header is invalid")
        stream.seek(0)
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    actual = digest.hexdigest()
    if actual.lower() != expected.lower():
        raise PreparationError("Source PDF no longer matches MinerU provenance")
    return path, actual


def _write_new_file(path: Path, data: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def load_title_overrides(path: Path) -> dict[str, dict[str, str]]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise PreparationError(f"Cannot read title overrides: {path}") from exc
    if (not isinstance(payload, dict) or set(payload) != {"version", "overrides"}
            or type(payload["version"]) is not int or payload["version"] != 1
            or not isinstance(payload["overrides"], list)):
        raise PreparationError("Title overrides need version 1 and an overrides array")
    result: dict[str, dict[str, str]] = {}
    required = {"document_id", "title", "source", "source_url", "verified_on"}
    for row in payload["overrides"]:
        if not isinstance(row, dict) or set(row) != required:
            raise PreparationError("Each title override needs document_id, title, source, source_url and verified_on")
        if any(not isinstance(row[key], str) or not row[key].strip() for key in required):
            raise PreparationError("Title override fields must be nonempty strings")
        document_id = row["document_id"]
        if document_id in result:
            raise PreparationError(f"Duplicate title override for {document_id}")
        result[document_id] = {key: row[key].strip() for key in required - {"document_id"}}
    return result


def prepare(bundle: Path, output_root: Path = DEFAULT_OUTPUT_ROOT, *,
            target_tokens: int = 600, max_tokens: int = 900,
            include_references: bool = False, include_front_matter: bool = False,
            execute: bool = False, tokenizer_json: Path | None = None,
            title_overrides: dict[str, dict[str, str]] | None = None,
            zotero_provenance: dict[str, str] | None = None,
            paper_provenance: dict | None = None) -> dict:
    if target_tokens < 1 or max_tokens < target_tokens:
        raise PreparationError("Expected 1 <= target_tokens <= max_tokens")
    zotero_provenance = validate_zotero_provenance(zotero_provenance)
    paper_provenance = validate_paper_provenance(paper_provenance)
    bundle = Path(bundle).resolve()
    document = adapt_bundle(bundle)
    document_id = document.get("document_id")
    if not isinstance(document_id, str) or not DOCUMENT_ID_PATTERN.fullmatch(document_id):
        raise PreparationError("Document ID is unsafe for an artifact directory")
    override = (title_overrides or {}).get(document_id)
    if override is not None:
        if set(override) != {"title", "source", "source_url", "verified_on"}:
            raise PreparationError(f"Invalid curated title override for {document_id}")
        document["title"] = override["title"]
        provenance = document.setdefault("provenance", {})
        provenance.update({
            "title": override["title"],
            "title_source": "curated_metadata_override",
            "title_review_required": False,
            "title_override_source": override["source"],
            "title_override_source_url": override["source_url"],
            "title_override_verified_on": override["verified_on"],
        })
    if zotero_provenance is not None:
        provenance = document.setdefault("provenance", {})
        if not isinstance(provenance, dict):
            raise PreparationError("Canonical provenance must be an object")
        legacy_names = {
            "zotero_parent_key": "zoteroParentKey",
            "zotero_pdf_attachment_key": "zoteroPdfAttachmentKey",
            "zotero_collection_key": "zoteroCollectionKey",
            "zotero_server_id": "zoteroServerId",
        }
        for field, legacy_name in legacy_names.items():
            existing_values = (provenance.get(field), provenance.get(legacy_name))
            if any(existing is not None and existing != zotero_provenance[field]
                   for existing in existing_values):
                raise PreparationError(f"{field} conflicts with MinerU Canonical provenance")
        provenance.update(zotero_provenance)
    source_path, source_hash = _verify_source_pdf(document.get("provenance", {}), bundle)
    if paper_provenance is not None:
        if paper_provenance["source_pdf_sha256"] != source_hash:
            raise PreparationError("Public paper provenance conflicts with the source PDF")
        provenance = document.get("provenance")
        if not isinstance(provenance, dict):
            raise PreparationError("Canonical provenance must be an object")
        for field, item in paper_provenance.items():
            if provenance.get(field) is not None and provenance[field] != item:
                raise PreparationError("Public paper provenance conflicts with Canonical metadata")
        provenance.update(paper_provenance)
    try:
        counter = estimate_tokens if tokenizer_json is None else load_local_token_counter(tokenizer_json)
    except RuntimeError as exc:
        raise PreparationError(str(exc)) from exc
    chunks = chunk_document(document, target_tokens=target_tokens, max_tokens=max_tokens,
                            include_references=include_references,
                            include_front_matter=include_front_matter, token_counter=counter)
    if not chunks:
        raise PreparationError("Chunker returned no indexable content")
    if zotero_provenance is not None:
        for row in chunks:
            provenance = row.get("provenance")
            if not isinstance(provenance, dict):
                raise PreparationError("Chunk provenance must be an object")
            provenance.update(zotero_provenance)
    if paper_provenance is not None:
        for row in chunks:
            provenance = row.get("provenance")
            if not isinstance(provenance, dict):
                raise PreparationError("Chunk provenance must be an object")
            provenance.update(paper_provenance)
    canonical_bytes = _json_bytes(document)
    chunks_bytes = ("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                            for row in chunks)).encode("utf-8")
    policy = {"target_tokens": target_tokens, "max_tokens": max_tokens,
              "include_references": include_references,
              "include_front_matter": include_front_matter}
    if tokenizer_json is not None:
        policy["tokenizer"] = dict(counter.metadata)
    fingerprint = hashlib.sha256(canonical_bytes + b"\0" + _json_bytes(policy)).hexdigest()
    root = Path(output_root).resolve()
    parent = root / document_id
    target = parent / fingerprint[:20]
    middle_path = bundle / "middle_json.json"
    if is_file(middle_path):
        parse_artifacts = [middle_path]
    else:
        # Resolve inferred bundle artifacts from the already-open local bundle,
        # avoiding round-tripping Unicode filenames through provenance JSON.
        content_candidates = sorted(bundle.glob("*_content_list.json"))
        layout_local = bundle / "layout.json"
        if not content_candidates or not is_file(layout_local):
            raise PreparationError("New-style MinerU bundle is missing content_list/layout")
        parse_artifacts = [content_candidates[0], layout_local]
    manifest = {
        "version": 1, "status": "PREPARED", "document_id": document_id,
        "title": document["title"], "fingerprint": fingerprint,
        "title_source": document.get("provenance", {}).get("title_source"),
        "title_review_required": document.get("provenance", {}).get("title_review_required", False),
        "title_override_source": document.get("provenance", {}).get("title_override_source"),
        "title_override_source_url": document.get("provenance", {}).get("title_override_source_url"),
        "title_override_verified_on": document.get("provenance", {}).get("title_override_verified_on"),
        "bundle_path": str(bundle),
        "parse_artifacts_sha256": {str(path.name): hashlib.sha256(read_bytes(path)).hexdigest()
                                    for path in parse_artifacts},
        "source_pdf": str(source_path), "source_pdf_sha256": source_hash,
        "canonical_sha256": hashlib.sha256(canonical_bytes).hexdigest(),
        "chunks_sha256": hashlib.sha256(chunks_bytes).hexdigest(),
        "chunk_count": len(chunks), "chunk_policy": policy,
    }
    if zotero_provenance is not None:
        manifest["zotero_provenance"] = zotero_provenance
    if paper_provenance is not None:
        manifest["paper_provenance"] = paper_provenance
    files = {"canonical.json": canonical_bytes, "chunks.jsonl": chunks_bytes,
             "manifest.json": _json_bytes(manifest)}
    summary = {"document_id": document_id, "title": document["title"],
               "chunks": len(chunks), "source_pdf_sha256": source_hash,
               "target_dir": str(target), "chunks_path": str(target / "chunks.jsonl"),
               "fingerprint": fingerprint}
    if not execute:
        return {"mode": "dry-run", **summary}
    parent.mkdir(parents=True, exist_ok=True)
    if parent.resolve().parent != root:
        raise PreparationError("Artifact parent escapes the configured output root")
    if target.exists():
        if target.is_symlink() or not target.is_dir():
            raise PreparationError("Existing artifact target is not a regular directory")
        for name, expected in files.items():
            existing = target / name
            if existing.is_symlink() or not existing.is_file() or existing.read_bytes() != expected:
                raise PreparationError("Existing prepared artifact differs; manual review required")
        return {"mode": "exists", **summary}
    stage = parent / f".prepare-{uuid.uuid4().hex}"
    stage.mkdir(exist_ok=False)
    try:
        for name, data in files.items():
            _write_new_file(stage / name, data)
        validated = load_bundle(stage / "chunks.jsonl")
        if validated["document_id"] != document_id or len(validated["rows"]) != len(chunks):
            raise PreparationError("Prepared chunks failed importer preflight")
        if target.exists():
            raise PreparationError("Artifact target appeared during preparation")
        stage.rename(target)
    finally:
        if stage.exists():
            if stage.resolve().parent != parent.resolve():
                raise PreparationError("Refusing cleanup outside the artifact parent")
            shutil.rmtree(stage)
    return {"mode": "prepared", **summary}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path, help="One existing MinerU output directory")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--target-tokens", type=int, default=600)
    parser.add_argument("--max-tokens", type=int, default=900)
    parser.add_argument("--include-references", action="store_true")
    parser.add_argument("--include-front-matter", action="store_true")
    parser.add_argument("--tokenizer-json", type=Path, help="Local tokenizer JSON; no model download")
    parser.add_argument("--title-overrides", type=Path,
                        help="Version 1 JSON of verified per-document title corrections")
    parser.add_argument("--execute", action="store_true", help="Write versioned local artifacts")
    args = parser.parse_args(argv)
    try:
        title_overrides = load_title_overrides(args.title_overrides) if args.title_overrides else None
        result = prepare(args.bundle, args.output_root, target_tokens=args.target_tokens,
                         max_tokens=args.max_tokens, include_references=args.include_references,
                         include_front_matter=args.include_front_matter, execute=args.execute,
                         tokenizer_json=args.tokenizer_json, title_overrides=title_overrides)
    except (PreparationError, ImportError_, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
