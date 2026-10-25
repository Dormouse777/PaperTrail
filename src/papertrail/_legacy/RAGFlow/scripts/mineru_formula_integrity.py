"""Local formula transport checks, NOT mathematical correctness validation.

No I/O, parser invocation, credentials, or network. Source text is retained even
when the parser introduced an incorrect symbol; such defects need review.
"""
from __future__ import annotations

import hashlib
from collections import Counter
from typing import Any


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(part for item in value if (part := _text(item)))
    if isinstance(value, dict):
        return _text(value.get("content"))
    return ""


def verify_equation_source(block: dict[str, Any]) -> str:
    """Require a readable, unchanged canonical formula with source content."""
    block_id = block.get("block_id")
    text = block.get("text")
    if not isinstance(block_id, str) or not block_id or not isinstance(text, str) or not text.strip():
        raise ValueError("Equation requires a source ID and nonempty text")
    text = text.strip()
    if _text(block.get("content_items")).strip() != text:
        raise ValueError(f"Equation canonical/source content mismatch: {block_id}")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if "equation_source_sha256" in block and block["equation_source_sha256"] != digest:
        raise ValueError(f"Equation source hash mismatch: {block_id}")
    return text


def verify_equation_delivery(blocks: list[dict[str, Any]], chunks: list[dict[str, Any]]) -> None:
    """Fail on missing, duplicated, changed, split, or mislocated formulas.

`blocks` must be the actual eligible equations after explicit section filters.
This proves text/locator conservation only, not source authentication or math.
"""
    ids: set[str] = set()
    required_per_chunk: dict[int, Counter] = {}
    for block in blocks:
        text = verify_equation_source(block)
        block_id = block["block_id"]
        if block_id in ids:
            raise ValueError(f"Duplicate equation source ID: {block_id}")
        ids.add(block_id)
        matches = [(i, chunk) for i, chunk in enumerate(chunks)
                   if block_id in chunk.get("mineru_block_ids", [])]
        if len(matches) != 1:
            raise ValueError(f"Equation must occur in exactly one chunk: {block_id}")
        i, chunk = matches[0]
        if chunk["mineru_block_ids"].count(block_id) != 1:
            raise ValueError(f"Duplicate equation chunk ID: {block_id}")
        locations = [loc for loc in chunk.get("locations", []) if loc.get("block_id") == block_id]
        if len(locations) != 1 or any(locations[0].get(key) != block.get(key)
                                      for key in ("page_index", "bbox", "source_type")):
            raise ValueError(f"Equation source location mismatch: {block_id}")
        if locations[0].get("segment_index") != 0:
            raise ValueError(f"Equation was split: {block_id}")
        raw = chunk.get("raw_text")
        if not isinstance(raw, str) or text not in raw:
            raise ValueError(f"Equation text missing from chunk: {block_id}")
        embedding = chunk.get("embedding_text")
        if not isinstance(embedding, str) or text not in embedding:
            raise ValueError(f"Equation text missing from embedding payload: {block_id}")
        required_per_chunk.setdefault(i, Counter())[text] += 1
    for i, counts in required_per_chunk.items():
        for text, count in counts.items():
            if chunks[i]["raw_text"].count(text) < count:
                raise ValueError("Repeated equation source text was collapsed")
