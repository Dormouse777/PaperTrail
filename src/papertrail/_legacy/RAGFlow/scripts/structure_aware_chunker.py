"""Build traceable retrieval chunks from CanonicalDocument JSON.

The default token counter is a deterministic estimate (Unicode word/punctuation
units, with each CJK character counted separately). Replace it with the target
embedding model tokenizer before treating the limits as exact model tokens.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from mineru_formula_integrity import verify_equation_delivery, verify_equation_source


TOKEN_RE = re.compile(r"[\u3400-\u9fff]|[\w]+(?:['’][\w]+)*|[^\s\w]", re.UNICODE)
SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?。！？])(?:\s+|(?=[\u3400-\u9fff]))|\n+")
NUMBERED_HEADING_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\s*(?:[|.)、:-]\s*)?(.*?)\s*$")
FIGURE_LABEL_RE = re.compile(
    r"^(?:(?:Extended\s+Data|Supplementary|Supplemental)\s+)?Fig(?:ure)?\.?\s+S?\d+\b", re.I)

PRIMARY_SECTIONS = {
    "abstract": "ABSTRACT",
    "introduction": "INTRODUCTION",
    "background": "INTRODUCTION",
    "methods": "METHODS",
    "star+methods": "METHODS",
    "materials and methods": "METHODS",
    "methods and materials": "METHODS",
    "experimental procedures": "METHODS",
    "experimental procedure": "METHODS",
    "results": "RESULTS",
    "discussion": "DISCUSSION",
    "conclusion": "CONCLUSION",
    "conclusions": "CONCLUSION",
    "references": "REFERENCES",
    "bibliography": "REFERENCES",
    "literature cited": "REFERENCES",
    "supplementary information": "SUPPLEMENTARY",
    "supporting information": "SUPPLEMENTARY",
    "supplemental information": "SUPPLEMENTARY",
    "acknowledgments": "OTHER",
    "acknowledgements": "OTHER",
    "conflict of interest": "OTHER",
    "conflicts of interest": "OTHER",
    "author contributions": "OTHER",
    "data availability": "OTHER",
    "data availability statement": "OTHER",
    "funding": "OTHER",
    "orcid": "OTHER",
}
OTHER_TOPLEVEL_HEADINGS = {
    "acknowledgments", "acknowledgements", "conflict of interest",
    "conflicts of interest", "author contributions", "data availability",
    "data availability statement", "funding", "orcid",
}


def estimate_tokens(text: str) -> int:
    """Stable, tokenizer-independent estimate; not an embedding tokenizer."""
    return len(TOKEN_RE.findall(text))


def load_local_token_counter(path: Path) -> Callable[[str], int]:
    """Load tokenizer JSON locally; never fetch models or execute model code.

    Counts plain embedding text with configured special tokens, without
    padding/truncation. Provider-side templates are not included.
    """
    resolved = Path(path).resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("Tokenizer path must be a local JSON file")
    payload = resolved.read_bytes()
    try:
        from tokenizers import Tokenizer
    except ImportError as exc:
        raise RuntimeError("Local tokenizer requires the tokenizers package") from exc
    tokenizer = Tokenizer.from_buffer(payload)
    tokenizer.no_padding()
    tokenizer.no_truncation()

    def count(text: str) -> int:
        return len(tokenizer.encode(text, add_special_tokens=True).ids)

    count.__name__ = "local_tokenizer_json"
    count.metadata = {
        "tokenizer_sha256": hashlib.sha256(payload).hexdigest(),
        "add_special_tokens": True,
        "padding": False,
        "truncation": False,
        "provider_template_verified": False,
    }
    return count


def _clean_heading(raw: str) -> tuple[str, str | None, int | None]:
    text = re.sub(r"\s+", " ", raw).strip()
    match = NUMBERED_HEADING_RE.match(text)
    if match:
        number, remainder = match.groups()
        # A number-only title should stay intact rather than becoming empty.
        clean = remainder.strip() or text
        return clean, number, len(number.split("."))
    return text, None, None


def _normalize_section(raw: str) -> str:
    normalized = re.sub(r"\s+", " ", raw).strip().casefold()
    return PRIMARY_SECTIONS.get(normalized, "OTHER")


def _is_references(raw: str) -> bool:
    clean, _, _ = _clean_heading(raw)
    return _normalize_section(clean) == "REFERENCES"


def _content_type(block: dict[str, Any]) -> str:
    source_type = str(block.get("source_type") or "")
    nested = {str(item.get("type")) for item in block.get("content_items", []) if isinstance(item, dict)}
    if source_type in {"table", "table_body", "table_caption"} or nested & {"table", "table_body", "table_caption"}:
        return "table"
    if source_type in {"equation", "display_equation"}:
        return "equation"
    if source_type in {"image", "chart"} or nested & {"image_body", "image_caption", "chart_body", "chart_caption"}:
        return "figure_caption" if block.get("text", "").strip() else "figure"
    if source_type in {"list", "list_item"} or nested & {"list", "list_item"}:
        return "list"
    return "paragraph"


def _typed_content_text(value: Any, wanted: set[str]) -> str:
    found: list[str] = []
    if isinstance(value, dict):
        if value.get("type") in wanted:
            text = str(value.get("content") or "").strip()
            if text:
                found.append(text)
        for child in value.values():
            found.append(_typed_content_text(child, wanted))
    elif isinstance(value, list):
        for child in value:
            found.append(_typed_content_text(child, wanted))
    return " ".join(text for text in found if text)


def _sentences(text: str) -> list[str]:
    parts: list[str] = []
    start = 0
    for match in SENTENCE_BOUNDARY_RE.finditer(text):
        part = text[start:match.end()]
        if part.strip():
            parts.append(part)
        start = match.end()
    tail = text[start:]
    if tail.strip():
        parts.append(tail)
    return parts or ([text] if text.strip() else [])


def _chunk_id(document_id: str, block_ids: list[str], text: str, ordinal: int) -> str:
    source = "\0".join((document_id, str(ordinal), *block_ids, text))
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]


@dataclass
class SemanticUnit:
    block: dict[str, Any]
    content_type: str
    text: str
    segment_index: int = 0
    oversize: bool = False

    @property
    def token_count(self) -> int:
        return estimate_tokens(self.text)


def _split_oversize_unit(
    block: dict[str, Any], max_tokens: int, token_counter: Callable[[str], int]
) -> list[SemanticUnit]:
    text = str(block.get("text") or "").strip()
    content_type = _content_type(block)
    if content_type == "equation":
        # LaTeX punctuation is not prose. Preserve the entire source formula;
        # downstream budget gates must handle the explicit oversize flag.
        text = verify_equation_source(block)
        return [SemanticUnit(block, content_type, text, oversize=token_counter(text) > max_tokens)]
    if token_counter(text) <= max_tokens:
        return [SemanticUnit(block, content_type, text)] if text else []
    sentences = _sentences(text)
    result: list[SemanticUnit] = []
    current: list[str] = []
    for sentence in sentences:
        candidate = "".join((*current, sentence))
        if current and token_counter(candidate) > max_tokens:
            result.append(SemanticUnit(block, content_type, "".join(current), len(result)))
            current = [sentence]
        else:
            current.append(sentence)
    if current:
        candidate = "".join(current)
        result.append(SemanticUnit(block, content_type, candidate, len(result), token_counter(candidate) > max_tokens))
    return result


def _leading_figure_tables(blocks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Associate a compact table panel with its sole later caption on that page.

    Long prose, other headings, multiple figure captions and cross-page matches
    are deliberately ambiguous. Source order and the original text stay intact.
    """
    pages: dict[int, list[dict[str, Any]]] = {}
    for block in blocks:
        if type(block.get("page_index")) is int:
            pages.setdefault(block["page_index"], []).append(block)
    result = {}
    ignored = {"header", "footer", "page_number", "aside_text", "page_footnote", "index"}
    for page in pages.values():
        headings = [i for i, b in enumerate(page)
                    if (b.get("type") == "heading" or type(b.get("caption_heading_level")) is int)
                    and FIGURE_LABEL_RE.match(str(b.get("text") or ""))]
        if len(headings) != 1:
            continue
        index = headings[0]
        leading = [b for b in page[:index] if b.get("source_type") not in ignored]
        if not any(_content_type(b) == "table" for b in leading):
            continue
        if any(b.get("type") == "heading" or type(b.get("caption_heading_level")) is int
               or _content_type(b) not in {"table", "paragraph"}
               or (_content_type(b) == "paragraph" and len(str(b.get("text") or "")) > 120)
               for b in leading):
            continue
        for block in leading:
            result[str(block.get("block_id"))] = page[index]
    return result


def _embedding_subsection(subsection: str | None) -> str | None:
    # The full source heading remains in metadata/evidence. Avoid repeating an
    # entire long caption in the embedding header as well as its raw body.
    label = FIGURE_LABEL_RE.match(subsection or "")
    return label.group() if label else subsection


def _assign_context(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Derive primary section/subsection hierarchy from titles and numbering."""
    annotated: list[dict[str, Any]] = []
    primary_raw = "Unsectioned"
    primary_normalized = "OTHER"
    subsection_raw: str | None = None
    references_active = False
    content_started = False
    leading_tables = _leading_figure_tables(blocks)

    for block in blocks:
        item = dict(block)
        caption = leading_tables.get(str(item.get("block_id")))
        if item.get("type") == "heading" or type(item.get("caption_heading_level")) is int or caption:
            raw = str((caption or item).get("text") or "").strip()
            if caption:
                item["scope_heading_block_id"] = caption["block_id"]
            if (caption or type(item.get("caption_heading_level")) is int) and references_active:
                # A numbered source caption ends the reference list, but does
                # not establish an author-supplied main section name.
                primary_raw, primary_normalized, subsection_raw = "Unsectioned", "OTHER", None
            clean, number, depth = _clean_heading(raw)
            normalized = _normalize_section(clean)
            if normalized == "REFERENCES":
                primary_raw = raw
                primary_normalized = "REFERENCES"
                subsection_raw = None
                references_active = True
            elif (
                normalized != "OTHER"
                or clean.casefold() in OTHER_TOPLEVEL_HEADINGS
                or (depth == 1 and number is not None)
            ):
                primary_raw = raw
                primary_normalized = normalized
                subsection_raw = None
                references_active = False
            else:
                # Numbered depth >=2 is a subsection. Unnumbered unknown
                # headings are also treated as subsections when a main section exists.
                if number is not None and depth == 1:
                    primary_raw = raw
                    primary_normalized = "OTHER"
                    subsection_raw = None
                    references_active = False
                else:
                    subsection_raw = raw
                    references_active = False
            item["heading_clean"] = clean
            item["heading_number"] = number
            item["section_raw"] = primary_raw
            item["section"] = primary_normalized
            item["subsection"] = subsection_raw
            item["excluded_from_index"] = references_active
            content_started = True
        else:
            item["section_raw"] = primary_raw
            item["section"] = primary_normalized
            item["subsection"] = subsection_raw
            item["excluded_from_index"] = references_active
        item["front_matter"] = not content_started and item.get("type") != "heading"
        annotated.append(item)
    return annotated


def _visual_related_context(blocks: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Link each figure/table to the nearest readable body block in its scope."""
    result: dict[str, list[dict[str, Any]]] = {}
    excluded = {"header", "footer", "page_number", "aside_text", "page_footnote", "index", "ref_text"}
    for index, block in enumerate(blocks):
        if _content_type(block) not in {"figure", "figure_caption", "table"}:
            continue
        related: list[dict[str, Any]] = []
        scope = (block.get("section"), block.get("subsection"))
        for step in (-1, 1):
            cursor = index + step
            while 0 <= cursor < len(blocks):
                candidate = blocks[cursor]
                if (candidate.get("section"), candidate.get("subsection")) != scope:
                    break
                if candidate.get("type") == "heading":
                    break
                source_type = str(candidate.get("source_type") or "")
                text = str(candidate.get("text") or "").strip()
                if (
                    source_type not in excluded
                    and candidate.get("type") != "title"
                    and _content_type(candidate) in {"paragraph", "list"}
                    and text
                ):
                    related.append({
                        "block_id": candidate.get("block_id"),
                        "page_index": candidate.get("page_index"),
                        "text": text[:500],
                    })
                    break
                cursor += step
        result[str(block.get("block_id"))] = related
    return result


def chunk_document(
    document: dict[str, Any],
    *,
    target_tokens: int = 600,
    max_tokens: int = 900,
    include_references: bool = False,
    include_front_matter: bool = False,
    token_counter: Callable[[str], int] = estimate_tokens,
) -> list[dict[str, Any]]:
    """Chunk a CanonicalDocument while preserving section and source locations."""
    if target_tokens < 1 or max_tokens < target_tokens:
        raise ValueError("Require 1 <= target_tokens <= max_tokens")
    doc_id = str(document.get("document_id") or "")
    title = str(document.get("title") or "Untitled")
    pages = {page.get("page_index"): page for page in document.get("pages", [])}
    blocks = sorted(document.get("blocks", []), key=lambda block: block.get("order", 0))
    annotated = _assign_context(blocks)
    related_by_visual = _visual_related_context(annotated)

    groups: list[tuple[tuple[str, str | None], list[SemanticUnit], dict[str, Any]]] = []
    current_key: tuple[str, str | None] | None = None
    current_units: list[SemanticUnit] = []
    current_context: dict[str, Any] = {}
    eligible_equations: list[dict[str, Any]] = []

    def flush_group() -> None:
        nonlocal current_units, current_key, current_context
        if current_key is not None and current_units:
            groups.append((current_key, current_units, current_context))
        current_key, current_units, current_context = None, [], {}

    for block in annotated:
        if block.get("type") == "heading":
            flush_group()
            continue
        if block.get("type") == "title" or block.get("source_type") in {
            "header", "footer", "page_number", "aside_text", "page_footnote", "index"
        }:
            continue
        if block.get("front_matter") and not include_front_matter:
            continue
        if block.get("excluded_from_index") and not include_references:
            continue
        if _content_type(block) == "equation":
            verify_equation_source(block)  # Check before the empty-block skip.
            eligible_equations.append(block)
        text = str(block.get("text") or "").strip()
        assets = block.get("asset_paths") or []
        if not text and not assets:
            continue
        key = (str(block.get("section") or "OTHER"), block.get("subsection"))
        context = {
            "section": key[0],
            "section_raw": str(block.get("section_raw") or "Unsectioned"),
            "subsection": key[1],
        }
        if current_key != key:
            flush_group()
            current_key, current_context = key, context
        if not text and assets:
            text = "[MinerU image/chart asset; no caption text was extracted.]"
        for unit in _split_oversize_unit(block, max_tokens, token_counter):
            # Use the supplied counter consistently, including custom model tokenizers.
            count = token_counter(unit.text)
            if count > max_tokens:
                unit.oversize = True
            current_units.append(unit)
    flush_group()

    chunks: list[dict[str, Any]] = []
    seen_embedding_text: set[str] = set()
    ordinal = 0
    for (section, subsection), units, context in groups:
        embedding_subsection = _embedding_subsection(subsection)
        batch: list[SemanticUnit] = []
        batch_tokens = 0

        def emit() -> None:
            nonlocal ordinal, batch, batch_tokens
            if not batch:
                return
            raw_text = "\n\n".join(unit.text for unit in batch if unit.text)
            block_ids = list(dict.fromkeys(str(unit.block.get("block_id")) for unit in batch))
            locations: list[dict[str, Any]] = []
            asset_refs: list[str] = []
            for unit in batch:
                block = unit.block
                loc = {
                    "block_id": block.get("block_id"),
                    "page_index": block.get("page_index"),
                    "printed_page_number": pages.get(block.get("page_index"), {}).get("printed_page_number"),
                    "bbox": block.get("bbox"),
                    "source_type": block.get("source_type"),
                    "segment_index": unit.segment_index,
                }
                if block.get("scope_heading_block_id"):
                    loc["scope_heading_block_id"] = block["scope_heading_block_id"]
                if loc not in locations:
                    locations.append(loc)
                asset_refs.extend(str(path) for path in block.get("asset_paths", []))
            page_indexes = [loc["page_index"] for loc in locations if isinstance(loc.get("page_index"), int)]
            types = list(dict.fromkeys(unit.content_type for unit in batch))
            heading_display = " > ".join(value for value in (context["section_raw"], subsection) if value)
            chunk_block_ids = set(block_ids)
            related_text = []
            visual_elements: list[dict[str, Any]] = []
            visual_ids: set[str] = set()
            for unit in batch:
                if unit.content_type not in {"figure", "figure_caption", "table"}:
                    continue
                visual_id = str(unit.block.get("block_id"))
                visual_related = related_by_visual.get(visual_id, [])
                for related in visual_related:
                    if related["block_id"] not in chunk_block_ids and related not in related_text:
                        related_text.append(related)
                if visual_id in visual_ids:
                    continue
                visual_ids.add(visual_id)
                block = unit.block
                if unit.content_type == "table":
                    caption = _typed_content_text(block.get("content_items", []), {"table_caption"})
                    visual_elements.append({
                        "kind": "table",
                        "table_id": visual_id,
                        "caption": caption,
                        "table_content": str(block.get("text") or ""),
                        "page": block.get("page_index"),
                        "section": section,
                        "subsection": subsection,
                        "related_text": visual_related,
                    })
                else:
                    caption = _typed_content_text(block.get("content_items", []), {"image_caption", "chart_caption"})
                    if not caption:
                        caption = str(block.get("text") or "")
                    visual_elements.append({
                        "kind": "figure",
                        "figure_id": visual_id,
                        "caption": caption,
                        "page": block.get("page_index"),
                        "section": section,
                        "subsection": subsection,
                        "asset_paths": block.get("asset_paths", []),
                        "related_text": visual_related,
                    })
            embedding_text = (
                f"Paper: {title}\nSection: {section}\n"
                + (f"Subsection: {embedding_subsection}\n" if embedding_subsection else "")
                + f"Content:\n{raw_text}"
            )
            if related_text:
                embedding_text += "\nRelated figure/table context:\n" + "\n".join(item["text"] for item in related_text)
            embedding_related_context_omitted = False
            if token_counter(embedding_text) > max_tokens and related_text:
                embedding_text = (
                    f"Paper: {title}\nSection: {section}\n"
                    + (f"Subsection: {embedding_subsection}\n" if embedding_subsection else "")
                    + f"Content:\n{raw_text}"
                )
                embedding_related_context_omitted = True
            if embedding_text in seen_embedding_text:
                # RAGFlow rejects duplicate embedding payloads within one paper.
                # Preserve the scientific text and add only deterministic location
                # metadata so repeated headers/captions remain independently traceable.
                embedding_text += "\nChunk location: " + ",".join(block_ids)
            seen_embedding_text.add(embedding_text)
            embedding_token_count = token_counter(embedding_text)
            chunk = {
                "chunk_id": _chunk_id(doc_id, block_ids, raw_text, ordinal),
                "document_id": doc_id,
                "title": title,
                "section": section,
                "section_raw": context["section_raw"],
                "subsection": subsection,
                "heading_path": heading_display,
                "content_type": types[0] if len(types) == 1 else "mixed",
                "content_types": types,
                "page_start": min(page_indexes) if page_indexes else None,
                "page_end": max(page_indexes) if page_indexes else None,
                "mineru_block_ids": block_ids,
                "locations": locations,
                "asset_paths": list(dict.fromkeys(asset_refs)),
                "related_text": related_text,
                "visual_elements": visual_elements,
                "raw_text": raw_text,
                "embedding_text": embedding_text,
                "token_count": embedding_token_count,
                "content_token_count": token_counter(raw_text),
                "token_count_method": getattr(token_counter, "__name__", "custom"),
                "target_tokens": target_tokens,
                "max_tokens": max_tokens,
                "oversize_atomic_unit": any(unit.oversize for unit in batch) or embedding_token_count > max_tokens,
                "embedding_related_context_omitted": embedding_related_context_omitted,
                "index_references": include_references,
                "provenance": {
                    "source_pdf": document.get("provenance", {}).get("sourcePdf"),
                    "source_pdf_sha256": document.get("provenance", {}).get("sourcePdfSha256"),
                    "zotero_parent_key": document.get("provenance", {}).get("zoteroParentKey"),
                    "zotero_pdf_attachment_key": document.get("provenance", {}).get("zoteroPdfAttachmentKey"),
                    "mineru_version": document.get("provenance", {}).get("minerUVersion"),
                    "parser_schema": document.get("provenance", {}).get("parser_schema"),
                    "parser_schema_version": document.get("provenance", {}).get("parser_schema_version"),
                },
            }
            if hasattr(token_counter, "metadata"):
                chunk["tokenizer_metadata"] = dict(token_counter.metadata)
            if "equation" in types:
                chunk["formula_review_required"] = True
                chunk["formula_semantics_verified"] = False
            chunks.append(chunk)
            ordinal += 1
            batch, batch_tokens = [], 0

        prefix = (
            f"Paper: {title}\nSection: {section}\n"
            + (f"Subsection: {embedding_subsection}\n" if embedding_subsection else "")
            + "Content:\n"
        )
        for unit in units:
            candidate_text = "\n\n".join([*(u.text for u in batch), unit.text])
            candidate_tokens = token_counter(prefix + candidate_text)
            if batch and candidate_tokens > max_tokens:
                emit()
            batch.append(unit)
            current_text = "\n\n".join(u.text for u in batch)
            if token_counter(prefix + current_text) >= target_tokens:
                emit()
        emit()
    verify_equation_delivery(eligible_equations, chunks)
    return chunks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("canonical_json", type=Path)
    parser.add_argument("--output", type=Path, required=True, help="Output JSONL path")
    parser.add_argument("--target-tokens", type=int, default=600)
    parser.add_argument("--max-tokens", type=int, default=900)
    parser.add_argument("--include-references", action="store_true")
    parser.add_argument("--include-front-matter", action="store_true")
    parser.add_argument("--tokenizer-json", type=Path,
                        help="Local tokenizer JSON; requires tokenizers, never downloads models")
    args = parser.parse_args()
    token_counter = estimate_tokens if args.tokenizer_json is None else load_local_token_counter(args.tokenizer_json)
    document = json.loads(args.canonical_json.read_text(encoding="utf-8"))
    chunks = chunk_document(
        document,
        target_tokens=args.target_tokens,
        max_tokens=args.max_tokens,
        include_references=args.include_references,
        include_front_matter=args.include_front_matter,
        token_counter=token_counter,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(chunk, ensure_ascii=False) + "\n" for chunk in chunks)
    args.output.write_text(payload, encoding="utf-8", newline="\n")
    print(json.dumps({"document_id": document.get("document_id"), "chunks": len(chunks), "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
