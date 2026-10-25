#!/usr/bin/env python3
"""Convert one explicitly selected local PDF with the loopback MinerU V1 API.

Preview is the default. The source PDF is never moved or modified. Execution is
restricted to the local MinerU service and publishes a validated, hash-keyed
bundle atomically beneath the selected output root.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
import shutil
import sys
import uuid
from pathlib import Path

from workflow_configuration import default_path
from mineru_adapter import adapt_bundle
from zotero_mineru_batch import (
    MINERU_BASE,
    download_output,
    get_json,
    parse_batch,
    safe_extract_bundle,
    upload_pdf,
)


DEFAULT_OUTPUT_ROOT = default_path("bundle_root", "data/bundles")


class ConversionError(RuntimeError):
    pass


def _regular_artifact(path: Path) -> None:
    """Reject links/junctions before opening a bundle-owned artifact."""
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or not stat.S_ISREG(info.st_mode)):
        raise ConversionError("Bundle artifact is not a regular local file")


def _regular_directory(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ConversionError("Bundle directory is a link or unsupported file type")


def _artifact_snapshot(bundle: Path, markdown_name: str) -> dict[str, str]:
    """Hash the known parse products; never open paths supplied by a manifest."""
    _regular_directory(bundle)
    products = [bundle / markdown_name, bundle / "middle_json.json"]
    images = bundle / "images"
    if images.exists() or images.is_symlink():
        def walk(directory: Path) -> None:
            _regular_directory(directory)
            for child in sorted(directory.iterdir()):
                info = child.lstat()
                if (stat.S_ISLNK(info.st_mode)
                        or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                    raise ConversionError("Bundle image tree contains a link")
                if stat.S_ISDIR(info.st_mode):
                    walk(child)
                else:
                    products.append(child)
                if len(products) > 10000:
                    raise ConversionError("Bundle artifact inventory exceeds the local limit")
        walk(images)
    result = {}
    for path in products:
        _regular_artifact(path)
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for part in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(part)
        result[path.relative_to(bundle).as_posix()] = digest.hexdigest()
    if (bundle / markdown_name).stat().st_size == 0:
        raise ConversionError("Bundle Markdown is empty")
    return result


def _reuse_integrity(target: Path, source: Path, source_hash: str, bundle_id: str) -> tuple[dict, str]:
    """Verify existing outputs without modifying them or resubmitting a PDF."""
    try:
        manifest_path = target / "provenance.json"
        _regular_artifact(manifest_path)
        if manifest_path.stat().st_size > 1024 * 1024:
            raise ConversionError("Bundle provenance exceeds the local limit")
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ConversionError("Duplicate bundle provenance field")
                result[key] = value
            return result
        manifest_bytes = manifest_path.read_bytes()
        def reject_constant(_value):
            raise ConversionError("Non-finite bundle provenance value")
        manifest = json.loads(manifest_bytes.decode("utf-8"),
                              object_pairs_hook=unique_object, parse_constant=reject_constant)
        if (not isinstance(manifest, dict)
                or manifest.get("sourcePdfSha256", "").lower() != source_hash.lower()
                or manifest.get("documentId") != bundle_id):
            raise ConversionError("Existing bundle source identity differs")
        # A recovered Zotero original can have a different filename. Preserve
        # the verified bundle basename instead of requiring the old PDF path.
        original = manifest.get("sourcePdfFilename", source.name)
        if not isinstance(original, str) or Path(original).name != original or not original.lower().endswith(".pdf"):
            raise ConversionError("Existing bundle source filename is unsafe")
        actual = _artifact_snapshot(target, Path(original).stem + ".md")
        expected = manifest.get("bundleArtifactsSha256")
        if "bundleArtifactsSha256" in manifest:
            if (not isinstance(expected, dict) or expected != actual):
                raise ConversionError("Existing bundle artifacts differ from recorded hashes")
            integrity = "artifact_hashes_verified"
        else:
            # Legacy files cannot acquire a historical integrity proof today.
            integrity = "legacy_structural_only"
        document = adapt_bundle(target)
        if (document.get("document_id") != bundle_id
                or not document.get("pages") or not document.get("blocks")
                or document.get("provenance", {}).get("sourcePdfSha256", "").lower() != source_hash.lower()):
            raise ConversionError("Existing bundle structure/source binding is invalid")
        if (_artifact_snapshot(target, Path(original).stem + ".md") != actual
                or manifest_path.read_bytes() != manifest_bytes):
            raise ConversionError("Bundle changed during reuse verification")
        return manifest, integrity
    except (OSError, ValueError, TypeError, AttributeError, RecursionError) as exc:
        raise ConversionError("Existing bundle is missing, malformed, or incompatible; manual review required") from None


def inspect_pdf(path: Path) -> tuple[Path, str]:
    source = Path(path).resolve(strict=True)
    if not source.is_file() or source.suffix.lower() != ".pdf":
        raise ConversionError("Input must be an existing PDF file")
    if source.stat().st_size < 1024:
        raise ConversionError("PDF is smaller than 1 KiB")
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise ConversionError("PDF header is invalid")
        stream.seek(0)
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return source, digest.hexdigest()


def convert(pdf: Path, output_root: Path = DEFAULT_OUTPUT_ROOT, *, tier: str = "standard",
            timeout_minutes: int = 90, execute: bool = False) -> dict:
    if tier not in {"flash", "basic", "standard", "advanced"}:
        raise ConversionError("Unsupported MinerU tier")
    if timeout_minutes < 1:
        raise ConversionError("timeout_minutes must be at least 1")
    source, source_hash = inspect_pdf(pdf)
    root = Path(output_root).resolve()
    parent = root / "_direct"
    bundle_id = source_hash[:20]
    target = parent / bundle_id
    # lstat also sees dangling links; they must not become a new parse request.
    try:
        parent.lstat()
    except FileNotFoundError:
        pass
    else:
        _regular_directory(parent)
    try:
        target.lstat()
        target_present = True
    except FileNotFoundError:
        target_present = False
    summary = {
        "source_pdf": str(source), "source_pdf_sha256": source_hash,
        "document_id": bundle_id, "title": source.stem,
        "target_dir": str(target), "mineru_base": MINERU_BASE,
    }
    if target_present:
        if target.is_symlink() or not target.is_dir():
            raise ConversionError("Existing output target is not a regular directory")
        manifest, integrity = _reuse_integrity(target, source, source_hash, bundle_id)
        markdown_path = target / (Path(manifest.get("sourcePdfFilename", source.name)).stem + ".md")
        return {"mode": "exists", **summary,
                "markdown": str(markdown_path), "bundle_integrity": integrity,
                "mineru_version": manifest.get("mineruVersion")}
    if not execute:
        return {"mode": "dry-run", **summary}

    parent.mkdir(parents=True, exist_ok=True)
    health, _ = get_json(f"{MINERU_BASE}/v1/health")
    candidate = {"pdf": source, "pdfFilename": source.name,
                 "sourcePdfSha256": source_hash}
    job = parse_batch([candidate], tier, timeout_minutes)
    file_result = next((item for item in job.get("files", [])
                        if item.get("file_id") == candidate.get("mineruFileId")), None)
    if not file_result:
        file_result = next((item for item in job.get("files", [])
                            if item.get("name") == source.name), None)
    if not file_result or file_result.get("status") != "completed":
        raise ConversionError("MinerU did not complete the selected PDF")
    zip_id = ((file_result.get("output_files") or {}).get("zip") or {}).get("file_id")
    if not zip_id:
        raise ConversionError("MinerU response has no ZIP result for the selected PDF")

    stage = parent / f".{bundle_id}.staging-{uuid.uuid4().hex}"
    stage.mkdir(exist_ok=False)
    try:
        archive = stage / ".result.zip"
        download_output(zip_id, archive)
        markdown, headings, refs, images = safe_extract_bundle(archive, stage)
        archive.unlink()
        markdown_path = stage / (source.stem + ".md")
        (stage / "markdown.md").replace(markdown_path)
        provenance = {
            "title": source.stem, "sourceType": "local_pdf", "documentId": bundle_id,
            "sourcePdf": str(source), "sourcePdfFilename": source.name,
            "sourcePdfSha256": source_hash,
            "markdown": str(target / markdown_path.name),
            "mineruVersion": health.get("version"), "mineruJobId": job.get("job_id"),
            "sections": headings, "imageReferenceCount": refs,
            "imageAssetCount": images,
            "bundleArtifactsSha256": _artifact_snapshot(stage, markdown_path.name),
        }
        (stage / "provenance.json").write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
        document = adapt_bundle(stage)
        if document.get("document_id") != bundle_id:
            raise ConversionError("Adapted MinerU bundle identity does not match PDF hash")
        if target.exists():
            raise ConversionError("Output target appeared during conversion; refusing overwrite")
        stage.rename(target)
    finally:
        if stage.exists():
            if stage.resolve().parent != parent.resolve():
                raise ConversionError("Refusing cleanup outside the selected output directory")
            shutil.rmtree(stage)
    return {"mode": "converted", **summary,
            "bundle_integrity": "artifact_hashes_verified",
            "mineru_version": health.get("version"), "job_id": job.get("job_id"),
            "markdown": str(target / (source.stem + ".md")),
            "headings": len(headings), "image_references": refs, "image_assets": images}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, help="one explicit local PDF; no recursive discovery")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--tier", choices=("flash", "basic", "standard", "advanced"), default="standard")
    parser.add_argument("--timeout-minutes", type=int, default=90)
    parser.add_argument("--execute", action="store_true", help="submit this PDF to loopback MinerU V1 API")
    args = parser.parse_args(argv)
    try:
        result = convert(args.pdf, args.output_root, tier=args.tier,
                         timeout_minutes=args.timeout_minutes, execute=args.execute)
    except (ConversionError, OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
