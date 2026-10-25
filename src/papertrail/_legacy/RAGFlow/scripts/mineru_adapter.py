"""Adapt a verified MinerU 4.x middle_json bundle to CanonicalDocument.

This module is intentionally read-only. It does not call MinerU, Zotero,
RAGFlow, or any network service, and it never edits the input bundle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from windows_path_io import open_path


SUPPORTED_SCHEMAS = {("docvortex.middle", "2.0")}


def _text_from_value(value: Any) -> str:
    """Extract visible text recursively without inventing missing text."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(part for item in value if (part := _text_from_value(item)))
    if isinstance(value, dict):
        if "content" in value:
            return _text_from_value(value["content"])
        return ""
    return ""


def _content_items(block: dict[str, Any]) -> list[dict[str, Any]]:
    items = block.get("content")
    if isinstance(items, str):
        # docvortex.middle 2.0 equations use scalar LaTeX, not a span array.
        # Wrap without normalizing symbols or rewriting the parser's output.
        return [{"type": block.get("type"), "content": items}]
    return items if isinstance(items, list) else []


def _asset_paths(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key in ("image_path", "img_path", "path"):
            image_path = value.get(key)
            if isinstance(image_path, str) and image_path and ("image" in key or key != "path"):
                found.append(image_path)
        for child in value.values():
            found.extend(_asset_paths(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_asset_paths(child))
    return found


def _printed_page_number(page: dict[str, Any]) -> str | None:
    for block in page.get("blocks", []):
        if block.get("type") != "page_number":
            continue
        text = _text_from_value(block.get("content")).strip()
        if text:
            return text
    return None


def _standard_type(source_type: str) -> str:
    return {
        "doc_title": "title",
        "paragraph_title": "heading",
        "text": "paragraph",
        "ref_text": "reference",
        "image": "figure",
        "chart": "figure",
        "page_footnote": "other",
        "aside_text": "other",
        "header": "other",
        "footer": "other",
        "page_number": "other",
        "index": "other",
    }.get(source_type, "other")


def _load_json(path: Path) -> dict[str, Any]:
    with open_path(path, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open_path(path, "rb") as handle:
        for part in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(part)
    return digest.hexdigest()


def _safe_document_id(bundle_dir: Path, pdf_path: Path) -> str:
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", pdf_path.stem).strip("-")
    return (stem[:48] + "-" + _sha256(pdf_path)[:12]).strip("-")[:64]


def _find_content_list(bundle_dir: Path) -> Path:
    candidates = sorted(bundle_dir.glob("*_content_list.json"))
    if not candidates:
        raise FileNotFoundError(f"Missing *_content_list.json: {bundle_dir}")
    return candidates[0]


_GENERIC_TITLE_KEYS = {
    "neuron", "neuroscience", "hhspublicaccess", "researcharticle",
    "openaccess", "article", "cellstemcell",
}
_MARKDOWN_HEADING_RE = re.compile(r"^[\ufeff \t]{0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_NUMBERED_HEADING_PREFIX_RE = re.compile(
    r"^\s*\d+(?:\.\d+)*(?:\s*[|.)、:-]\s*|\s+)"
)
_INCOMPLETE_TITLE_ENDINGS = re.compile(
    r"\b(?:and|or|of|with|by|for|from|to|in|on|after|via|through)$", re.IGNORECASE
)


def _title_key(value: str) -> str:
    return "".join(char.casefold() for char in value if char.isalnum())


def _clean_title_heading(value: str) -> str:
    text = re.sub(r"\s+", " ", value).strip(" \t\r\n#")
    text = re.sub(r"^(?:RESEARCH\s*ARTICLE|ARTICLE)\s+", "", text, flags=re.IGNORECASE)
    return text.strip()


def _heading_key(value: str) -> str:
    """Normalize a heading label, ignoring a numbered section prefix."""
    text = re.sub(r"\s+", " ", value).strip(" \t\r\n#")
    text = _NUMBERED_HEADING_PREFIX_RE.sub("", text).strip()
    return _title_key(text)


def _markdown_heading_levels(markdown: str) -> dict[str, int]:
    """Return normalized Markdown heading labels and their shallowest level.

    Markdown contributes heading labels and hierarchy only. The content-list
    remains the source of block text, order, page index, and bounding box.
    """
    levels: dict[str, int] = {}
    for line in markdown.splitlines():
        match = _MARKDOWN_HEADING_RE.match(line)
        if not match:
            continue
        key = _heading_key(match.group(2))
        if not key or key in _GENERIC_TITLE_KEYS:
            continue
        level = len(match.group(1))
        levels[key] = min(level, levels.get(key, level))
    return levels


def _effective_heading_level(text: str, content_list_level: Any,
                            markdown_heading_levels: dict[str, int]) -> int | None:
    """Prefer an exact Markdown heading level; otherwise retain MinerU's level."""
    markdown_level = markdown_heading_levels.get(_heading_key(text))
    if markdown_level is not None:
        return markdown_level
    return content_list_level if isinstance(content_list_level, int) else None


def _select_content_list_title(bundle_dir: Path, pdf_stem: str,
                                headings: list[str]) -> tuple[str, str, bool]:
    """Prefer the article-named source PDF; reject known MinerU page mastheads.

    A content-list's first level-one heading is often a journal or repository
    banner. The source filename is the stable article-title candidate for our
    normalized MinerU bundles. If it is generic, use the first non-banner
    heading; preserve an incomplete-title warning rather than inventing text.
    """
    source_title = re.sub(r"\s+", " ", pdf_stem).strip()
    source_key = _title_key(source_title)
    folder_title = re.sub(r"\s+", " ", bundle_dir.name).strip()
    folder_key = _title_key(folder_title)

    clean_headings = []
    for heading in headings:
        cleaned = _clean_title_heading(heading)
        key = _title_key(cleaned)
        if cleaned and key not in _GENERIC_TITLE_KEYS and cleaned not in clean_headings:
            clean_headings.append(cleaned)

    source_is_generic = source_key in _GENERIC_TITLE_KEYS
    source_is_incomplete = bool(_INCOMPLETE_TITLE_ENDINGS.search(source_title))
    folder_is_incomplete = bool(_INCOMPLETE_TITLE_ENDINGS.search(folder_title))

    # Prefer a non-truncated normalized folder title when it is nearly the same
    # title as the PDF basename; otherwise do not silently merge candidates.
    if (source_is_incomplete and not folder_is_incomplete and folder_key
            and SequenceMatcher(None, source_key, folder_key).ratio() >= 0.80):
        return folder_title, "bundle_directory_name", False

    if not source_is_generic and len(source_key) >= 5:
        return source_title, "source_pdf_filename", source_is_incomplete

    if not folder_key or folder_key in _GENERIC_TITLE_KEYS:
        folder_title = ""
    if folder_title and not folder_is_incomplete:
        return folder_title, "bundle_directory_name", False
    if clean_headings:
        selected = max(clean_headings, key=lambda item: len(_title_key(item)))
        return selected, "content_list_heading_fallback", bool(
            _INCOMPLETE_TITLE_ENDINGS.search(selected)
        )
    if folder_title:
        return folder_title, "bundle_directory_name", folder_is_incomplete
    return source_title, "source_pdf_filename", source_is_incomplete


def _title_heading_position(title: str, heading_items: list[tuple[int, str]]) -> int | None:
    """Find the heading representing the selected article title, if present."""
    target = _title_key(title)
    if not target:
        return None
    matches = []
    for position, heading in heading_items:
        cleaned = _clean_title_heading(heading)
        key = _title_key(cleaned)
        if not key or key in _GENERIC_TITLE_KEYS:
            continue
        matches.append((SequenceMatcher(None, target, key).ratio(), position))
    if not matches:
        return None
    score, position = min(matches, key=lambda match: (-match[0], match[1]))
    return position if score >= 0.82 else None


def _content_list_source_type(position: int, text: str, level: Any,
                              title_position: int | None,
                              markdown_heading_levels: dict[str, int] | None = None) -> str:
    key = _heading_key(text)
    if key in _GENERIC_TITLE_KEYS:
        return "header"
    if position == title_position or isinstance(level, int):
        # Keep the article heading as a boundary: the chunker uses headings to
        # end front-matter exclusion, which is essential when Abstract/Results
        # are not separately labeled in the MinerU content list.
        return "paragraph_title"
    if markdown_heading_levels and key in markdown_heading_levels:
        return "paragraph_title"
    return "text"


def _adapt_content_list_bundle(bundle_dir: Path) -> dict[str, Any]:
    content_path = _find_content_list(bundle_dir)
    layout_path = bundle_dir / "layout.json"
    if not layout_path.is_file():
        raise FileNotFoundError(f"Missing layout.json: {bundle_dir}")
    with open_path(content_path, "r", encoding="utf-8") as handle:
        content = json.load(handle)
    if not isinstance(content, list):
        raise ValueError("MinerU content_list must be an array")
    pdfs = sorted(bundle_dir.glob("*.pdf"))
    if not pdfs:
        raise FileNotFoundError(f"Missing source PDF: {bundle_dir}")
    pdf_path = pdfs[0]
    markdown_files = sorted(bundle_dir.glob("*.md"))
    markdown_path = markdown_files[0] if markdown_files else None
    markdown_heading_levels: dict[str, int] = {}
    if markdown_path:
        with open_path(markdown_path, "r", encoding="utf-8") as handle:
            markdown_heading_levels = _markdown_heading_levels(handle.read())
    heading_items = [(position, str(item.get("text", "")).strip())
                     for position, item in enumerate(content)
                     if isinstance(item, dict) and item.get("text_level") == 1
                     and str(item.get("text", "")).strip()]
    title_headings = [heading for _, heading in heading_items]
    title, title_source, title_review_required = _select_content_list_title(
        bundle_dir, pdf_path.stem, title_headings
    )
    if not title:
        title = markdown_path.stem if markdown_path else pdf_path.stem
        title_source = "markdown_or_pdf_filename_fallback"
    markdown_heading_items = [
        (position, str(item.get("text", "")).strip())
        for position, item in enumerate(content)
        if isinstance(item, dict)
        and _heading_key(str(item.get("text", "")).strip()) in markdown_heading_levels
        and str(item.get("text", "")).strip()
    ]
    title_position = _title_heading_position(title, heading_items + markdown_heading_items)
    document_id = _safe_document_id(bundle_dir, pdf_path)
    page_indices = sorted({item.get("page_idx", 0) for item in content if isinstance(item, dict)})
    pages = [{"page_index": page, "printed_page_number": None, "width_pt": None, "height_pt": None}
             for page in page_indices]
    blocks = []
    sections = []
    current_section = None
    order = 0
    for position, item in enumerate(content):
        if not isinstance(item, dict):
            continue
        text = _text_from_value(item.get("text") or item.get("table_body")
                                or item.get("img_caption") or item.get("image_caption")).strip()
        original_type = item.get("type")
        if original_type in {"equation", "display_equation"} and not text:
            raise ValueError(f"Missing or unsupported equation content: {document_id}:{position}")
        if not text:
            continue
        page_index = item.get("page_idx", 0)
        level = item.get("text_level")
        heading_key = _heading_key(text)
        heading_level = _effective_heading_level(
            text, level, markdown_heading_levels
        )
        source_type = _content_list_source_type(
            position, text, level, title_position, markdown_heading_levels
        )
        if original_type in {"image", "chart", "equation", "display_equation"}:
            source_type = original_type
        block_id = f"{document_id}:{page_index}:{position}"
        block = {"block_id": block_id, "page_index": page_index, "source_type": source_type,
                 "type": _standard_type(source_type), "text": text,
                 "heading_level": heading_level,
                 "bbox": item.get("bbox"), "order": order, "content_items": [item],
                 "asset_paths": sorted(set(_asset_paths(item)))}
        if source_type in {"equation", "display_equation"}:
            # Normalize the container, never the parser's LaTeX or symbols.
            block["content_items"] = [{**item, "content": text}]
            block["equation_source_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
            block["formula_semantics_verified"] = False
        if source_type in {"image", "chart"} and heading_key in markdown_heading_levels:
            # Keep a caption as evidence while retaining its explicit Markdown
            # heading boundary. A heading-only block would drop the caption.
            block["caption_heading_level"] = markdown_heading_levels[heading_key]
            block["caption_heading_source"] = "explicit_markdown_heading"
        elif source_type in {"image", "chart"} and re.match(
                r"^(?:(?:Extended\s+Data|Supplementary|Supplemental)\s+)?Fig(?:ure)?\.?\s+S?\d+\b",
                text, re.IGNORECASE):
            # Saved lists can label a figure after References without a Markdown
            # heading. Its own numbered caption establishes a new scope.
            block["caption_heading_level"] = 2
            block["caption_heading_source"] = "numbered_source_caption"
        blocks.append(block)
        order += 1
        if source_type in {"doc_title", "paragraph_title"} or "caption_heading_level" in block:
            current_section = {"section_raw": text, "section_normalized": None,
                               "heading_level": heading_level, "block_ids": [block_id],
                               "page_start": page_index, "page_end": page_index}
            sections.append(current_section)
        elif current_section is not None:
            current_section["block_ids"].append(block_id)
            current_section["page_end"] = page_index
    pdf_hash = _sha256(pdf_path)
    matched_markdown_heading_keys = {
        _heading_key(str(item.get("text", "")).strip())
        for item in content if isinstance(item, dict)
        and _heading_key(str(item.get("text", "")).strip()) in markdown_heading_levels
    }
    provenance = {"sourcePdf": str(pdf_path), "sourcePdfSha256": pdf_hash,
                  "title": title, "title_source": title_source,
                  "title_review_required": title_review_required,
                  "title_candidates": title_headings[:12], "documentId": document_id,
                  "markdown_heading_count": len(markdown_heading_levels),
                  "markdown_heading_matched_count": len(matched_markdown_heading_keys),
                  "source_provenance_inferred": True,
                  "bundle_path": str(bundle_dir),
                  "markdown_path": str(markdown_path) if markdown_path else None,
                  "content_list_path": str(content_path), "layout_path": str(layout_path),
                  "parser_schema": "mineru.content_list",
                  "parser_schema_version": "1.x"}
    return {"document_id": document_id, "title": title, "provenance": provenance,
            "pages": pages, "blocks": blocks, "sections": sections}


def adapt_bundle(bundle_dir: Path) -> dict[str, Any]:
    """Return a JSON-serializable CanonicalDocument for one MinerU bundle."""
    middle_path = bundle_dir / "middle_json.json"
    if not middle_path.is_file():
        return _adapt_content_list_bundle(bundle_dir)
    middle = _load_json(middle_path)
    schema_key = (middle.get("schema"), str(middle.get("schema_version")))
    if schema_key not in SUPPORTED_SCHEMAS:
        raise ValueError(
            f"Unsupported MinerU schema {schema_key!r}; supported: {sorted(SUPPORTED_SCHEMAS)}"
        )

    provenance_path = bundle_dir / "provenance.json"
    provenance = _load_json(provenance_path) if provenance_path.is_file() else {}
    markdown_files = sorted(bundle_dir.glob("*.md"))
    markdown_path = markdown_files[0] if markdown_files else None
    metadata = middle.get("metadata", {}).get("document", {})
    title = provenance.get("title") or metadata.get("title")
    if not title and markdown_path:
        title = markdown_path.stem
    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"Cannot determine document title: {bundle_dir}")

    document_id = str(provenance.get("documentId") or
                      provenance.get("zoteroPdfAttachmentKey") or bundle_dir.name)
    pages: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []
    sections: list[dict[str, Any]] = []
    current_section: dict[str, Any] | None = None
    order = 0
    raw_pages = middle.get("pages")
    if not isinstance(raw_pages, list):
        raise ValueError("middle_json.pages must be an array")

    layout_pages = middle.get("extensions", {}).get("docvortex_layout", {}).get("pages", [])
    layout_by_index = {
        item.get("page_idx"): item for item in layout_pages if isinstance(item, dict)
    }

    for page in raw_pages:
        if not isinstance(page, dict):
            continue
        page_index = page.get("page_idx")
        layout = layout_by_index.get(page_index, {})
        page_record = {
            "page_index": page_index,
            "printed_page_number": _printed_page_number(page),
            "width_pt": layout.get("width_pt"),
            "height_pt": layout.get("height_pt"),
        }
        pages.append(page_record)
        raw_blocks = page.get("blocks", [])
        if not isinstance(raw_blocks, list):
            continue
        for source_position, raw_block in enumerate(raw_blocks):
            if not isinstance(raw_block, dict):
                continue
            source_type = str(raw_block.get("type") or "other")
            source_index = raw_block.get("index", source_position)
            block_id = f"{document_id}:{page_index}:{source_index}"
            items = _content_items(raw_block)
            text = _text_from_value(items).strip()
            if source_type in {"equation", "display_equation"} and not text:
                raise ValueError(f"Missing or unsupported equation content: {block_id}")
            heading_level = raw_block.get("level")
            block = {
                "block_id": block_id,
                "page_index": page_index,
                "source_type": source_type,
                "type": _standard_type(source_type),
                "text": text,
                "heading_level": heading_level,
                "bbox": raw_block.get("bbox"),
                "order": order,
                "content_items": items,
                # Include explicit block-level bindings; never guess filenames.
                "asset_paths": sorted(set(_asset_paths(raw_block))),
            }
            if source_type in {"equation", "display_equation"}:
                block["equation_source_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
                block["formula_semantics_verified"] = False
            blocks.append(block)
            order += 1

            if source_type == "paragraph_title" and text:
                current_section = {
                    "section_raw": text,
                    "section_normalized": None,
                    "heading_level": heading_level,
                    "block_ids": [block_id],
                    "page_start": page_index,
                    "page_end": page_index,
                }
                sections.append(current_section)
            elif current_section is not None:
                current_section["block_ids"].append(block_id)
                current_section["page_end"] = page_index

    return {
        "document_id": document_id,
        "title": title,
        "provenance": {
            **provenance,
            "bundle_path": str(bundle_dir),
            "markdown_path": str(markdown_path) if markdown_path else None,
            "middle_json_path": str(middle_path),
            "parser_schema": middle.get("schema"),
            "parser_schema_version": str(middle.get("schema_version")),
        },
        "pages": pages,
        "blocks": blocks,
        "sections": sections,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path, help="One MinerU output bundle")
    parser.add_argument("--output", type=Path, help="Write CanonicalDocument JSON here")
    args = parser.parse_args()
    document = adapt_bundle(args.bundle)
    encoded = json.dumps(document, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8", newline="\n")
    else:
        print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
