"""Adapt a verified Springer Nature JATS XML artifact to CanonicalDocument.

This is a local, read-only XML adapter. It does not fetch papers, inspect
credentials, call MinerU, or treat XML as a PDF. Its XML support is deliberately
limited to UTF-8 JATS without DTD or ENTITY declarations and to 16 MiB inputs.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence
from urllib.parse import quote, urlsplit
import xml.etree.ElementTree as ET

from structure_aware_chunker import chunk_document


MAX_JATS_BYTES = 16 * 1024 * 1024
MAX_SIDECAR_BYTES = 64 * 1024
SOURCE_ID = "springer-nature-fulltext-jats"
FULLTEXT_ENDPOINT = "https://api.springernature.com/xmldata/jats"
FULLTEXT_DOCUMENTATION = "https://dev.springernature.com/docs/api-endpoints/fulltext-api/"
MIGRATION_DOCUMENTATION = "https://dev.springernature.com/docs/full-text-api-migration-guide/"
ALLOWED_SOURCE_STATUSES = {"downloaded_xml", "exists_xml"}
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_DOI_RE = re.compile(r"10\.\d{4,9}/[A-Z0-9._;()/:+\-]+\Z", re.IGNORECASE)
_DTD_OR_ENTITY_RE = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_XML_ENCODING_RE = re.compile(
    r"^\s*<\?xml\s+[^>]*?encoding\s*=\s*(['\"])([^'\"]+)\1", re.IGNORECASE
)
_SIDECAR_FIELDS = {
    "version", "resource_type", "doi", "source", "endpoint",
    "permission_evidence", "jats_product_evidence", "sha256", "size_bytes",
}


class JatsAdapterError(ValueError):
    """A stable, safe-to-report adapter failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise JatsAdapterError(code)


def _normalize_doi(value: Any) -> str:
    if not isinstance(value, str):
        _fail("DOI_INVALID")
    candidate = value.strip()
    if not candidate or any(ord(char) < 0x21 or ord(char) > 0x7E for char in candidate):
        _fail("DOI_INVALID")
    lowered = candidate.lower()
    if lowered.startswith(("https://", "http://")):
        try:
            parsed = urlsplit(candidate)
            if (parsed.scheme.lower() not in {"http", "https"}
                    or (parsed.hostname or "").lower() not in {"doi.org", "dx.doi.org"}
                    or parsed.username is not None or parsed.password is not None
                    or parsed.port is not None or parsed.query or parsed.fragment):
                _fail("DOI_INVALID")
            candidate = parsed.path.lstrip("/")
        except (TypeError, ValueError):
            _fail("DOI_INVALID")
    elif lowered.startswith("doi:"):
        candidate = candidate[4:].strip()
    if not _DOI_RE.fullmatch(candidate):
        _fail("DOI_INVALID")
    normalized = candidate.lower()
    if not normalized.startswith(("10.1007/", "10.1038/", "10.1186/")):
        _fail("DOI_PUBLISHER_MISMATCH")
    return normalized


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _element_text(node: ET.Element) -> str:
    return " ".join("".join(node.itertext()).split())


def _load_json_object(payload: str) -> dict[str, Any]:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    value = json.loads(payload, object_pairs_hook=reject_duplicate_keys)
    if not isinstance(value, dict):
        raise ValueError("JSON root is not an object")
    return value


def _is_public_https_reference(value: Any) -> bool:
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > 4096
            or any(ord(char) <= 0x20 or ord(char) == 0x7F for char in value)):
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return False
    if (parsed.scheme.lower() != "https" or not host or parsed.username is not None
            or parsed.password is not None or port not in (None, 443) or parsed.query):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        candidate = host.lower().rstrip(".")
        if (not candidate or candidate in {"localhost", "metadata.google.internal"}
                or candidate.endswith((".localhost", ".local", ".internal", ".test",
                                      ".invalid", ".onion"))):
            return False
        try:
            candidate.encode("idna").decode("ascii")
        except UnicodeError:
            return False
        return True
    return address.is_global


def _read_sidecar(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        _fail("JATS_SIDECAR_MISSING_OR_UNSAFE")
    try:
        if path.stat().st_size > MAX_SIDECAR_BYTES:
            _fail("JATS_SIDECAR_INVALID")
        return _load_json_object(path.read_text(encoding="utf-8"))
    except JatsAdapterError:
        raise
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        _fail("JATS_SIDECAR_INVALID")


def _parse_xml(payload: bytes) -> ET.Element:
    if not payload:
        _fail("JATS_XML_EMPTY")
    if len(payload) > MAX_JATS_BYTES:
        _fail("JATS_XML_SIZE_LIMIT_EXCEEDED")
    try:
        decoded = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        _fail("JATS_XML_ENCODING_UNSUPPORTED")
    if "\x00" in decoded:
        _fail("JATS_XML_ENCODING_UNSUPPORTED")
    declaration = _XML_ENCODING_RE.search(decoded)
    if (declaration is not None
            and declaration.group(2).lower().replace("_", "-") not in {"utf-8", "utf8"}):
        _fail("JATS_XML_ENCODING_UNSUPPORTED")
    if _DTD_OR_ENTITY_RE.search(decoded):
        _fail("JATS_XML_SECURITY_REJECTED")
    try:
        return ET.fromstring(payload)
    except (ET.ParseError, ValueError, LookupError):
        _fail("JATS_XML_INVALID")


def _article_parts(root: ET.Element, expected_doi: str) -> tuple[
    ET.Element, ET.Element, str, list[ET.Element]
]:
    articles = [node for node in root.iter() if _local_name(node.tag) == "article"]
    if len(articles) != 1:
        _fail("JATS_ARTICLE_COUNT_INVALID")
    article = articles[0]
    metadata = [node for node in article.iter() if _local_name(node.tag) == "article-meta"]
    doi_values = [
        node.text.strip()
        for meta in metadata for node in meta.iter()
        if (_local_name(node.tag) == "article-id"
            and str(node.attrib.get("pub-id-type", "")).lower() == "doi"
            and isinstance(node.text, str) and node.text.strip())
    ]
    if len(doi_values) != 1:
        _fail("JATS_ARTICLE_DOI_INVALID")
    if _normalize_doi(doi_values[0]) != expected_doi:
        _fail("JATS_DOI_MISMATCH")
    title_nodes = [node for meta in metadata for node in meta.iter()
                   if _local_name(node.tag) == "article-title" and _element_text(node)]
    if len(title_nodes) != 1:
        _fail("JATS_ARTICLE_TITLE_INVALID")
    bodies = [node for node in article.iter() if _local_name(node.tag) == "body"]
    if len(bodies) != 1:
        _fail("JATS_FULLTEXT_BODY_MISSING")
    body = bodies[0]
    paragraph_text = "".join(
        "".join(node.itertext()) for node in body.iter() if _local_name(node.tag) == "p"
    )
    if len(re.sub(r"\s+", "", paragraph_text)) < 40:
        _fail("JATS_FULLTEXT_BODY_MISSING")
    return article, body, _element_text(title_nodes[0]), metadata


def _validate_provenance(
    provenance: Mapping[str, Any],
) -> tuple[dict[str, Any], ET.Element, ET.Element, str, ET.Element]:
    if not isinstance(provenance, Mapping):
        _fail("JATS_PROVENANCE_INVALID")
    doi = _normalize_doi(provenance.get("doi"))
    if provenance.get("source_status") not in ALLOWED_SOURCE_STATUSES:
        _fail("JATS_SOURCE_STATUS_INVALID")
    if (provenance.get("resource_type") != "jats_xml"
            or provenance.get("source") != SOURCE_ID
            or provenance.get("source_parser") != "jats_adapter"
            or provenance.get("endpoint") != FULLTEXT_ENDPOINT):
        _fail("JATS_PROVENANCE_SOURCE_MISMATCH")
    for pdf_field in ("source_pdf", "sourcePdf", "source_pdf_sha256", "sourcePdfSha256"):
        if provenance.get(pdf_field) is not None:
            _fail("JATS_PROVENANCE_CLAIMS_PDF")

    xml_value = provenance.get("source_xml")
    if not isinstance(xml_value, str) or not xml_value:
        _fail("JATS_XML_PATH_INVALID")
    xml_path = Path(xml_value)
    if not xml_path.is_absolute() or not xml_path.name.lower().endswith(".jats.xml"):
        _fail("JATS_XML_PATH_INVALID")
    try:
        if xml_path.is_symlink() or not xml_path.is_file():
            _fail("JATS_XML_MISSING_OR_UNSAFE")
        resolved_xml = xml_path.resolve(strict=True)
        if str(resolved_xml) != xml_value:
            _fail("JATS_XML_PATH_INVALID")
        sidecar_path = Path(str(resolved_xml) + ".provenance.json")
        if sidecar_path.is_symlink() or not sidecar_path.is_file():
            _fail("JATS_SIDECAR_MISSING_OR_UNSAFE")
        resolved_sidecar = sidecar_path.resolve(strict=True)
        if str(resolved_sidecar) != provenance.get("source_sidecar"):
            _fail("JATS_SIDECAR_PATH_MISMATCH")
        size_bytes = resolved_xml.stat().st_size
        if size_bytes > MAX_JATS_BYTES:
            _fail("JATS_XML_SIZE_LIMIT_EXCEEDED")
        payload = resolved_xml.read_bytes()
        sidecar = _read_sidecar(resolved_sidecar)
    except JatsAdapterError:
        raise
    except OSError:
        _fail("JATS_ARTIFACT_READ_FAILED")

    if set(sidecar) != _SIDECAR_FIELDS:
        _fail("JATS_SIDECAR_SCHEMA_INVALID")
    permission = sidecar.get("permission_evidence")
    product = sidecar.get("jats_product_evidence")
    if (sidecar.get("version") != 1 or type(sidecar.get("version")) is not int
            or sidecar.get("resource_type") != "jats_xml"
            or sidecar.get("source") != SOURCE_ID
            or sidecar.get("endpoint") != FULLTEXT_ENDPOINT
            or not _is_public_https_reference(permission)
            or urlsplit(permission).hostname.lower() == "dev.springernature.com"
            or product not in {FULLTEXT_DOCUMENTATION, MIGRATION_DOCUMENTATION}):
        _fail("JATS_SIDECAR_EVIDENCE_INVALID")
    sidecar_doi = _normalize_doi(sidecar.get("doi"))
    if sidecar_doi != doi:
        _fail("JATS_SIDECAR_DOI_MISMATCH")
    expected_hash = sidecar.get("sha256")
    if not isinstance(expected_hash, str) or not _SHA256_RE.fullmatch(expected_hash):
        _fail("JATS_SIDECAR_HASH_INVALID")
    actual_hash = hashlib.sha256(payload).hexdigest()
    if (expected_hash != actual_hash or type(sidecar.get("size_bytes")) is not int
            or sidecar.get("size_bytes") != len(payload) or len(payload) != size_bytes):
        _fail("JATS_XML_HASH_OR_SIZE_MISMATCH")

    bound_fields = {
        "resource_type": "jats_xml",
        "source": SOURCE_ID,
        "doi": doi,
        "endpoint": FULLTEXT_ENDPOINT,
        "permission_evidence": permission,
        "jats_product_evidence": product,
        "source_xml_sha256": actual_hash,
    }
    if any(provenance.get(key) != value for key, value in bound_fields.items()):
        _fail("JATS_PROVENANCE_SIDECAR_MISMATCH")
    if not isinstance(provenance.get("source_xml_sha256"), str):
        _fail("JATS_PROVENANCE_SIDECAR_MISMATCH")

    root = _parse_xml(payload)
    article, body, title, _ = _article_parts(root, doi)
    verified = {
        "resource_type": "jats_xml",
        "source": SOURCE_ID,
        "source_parser": "jats_adapter",
        "source_status": provenance["source_status"],
        "source_xml": str(resolved_xml),
        "source_sidecar": str(resolved_sidecar),
        "source_xml_sha256": actual_hash,
        "doi": doi,
        "endpoint": FULLTEXT_ENDPOINT,
        "permission_evidence": permission,
        "jats_product_evidence": product,
        "size_bytes": len(payload),
        "title_source": "jats_article_title",
        "parser_schema": "jats",
        "parser_schema_version": next(
            (node.attrib.get("dtd-version") for node in article.iter()
             if _local_name(node.tag) == "article"), None
        ),
        "source_pdf": None,
    }
    return verified, root, article, title, body


def validate_jats_provenance(provenance: Mapping[str, Any]) -> dict[str, Any]:
    """Re-read and verify local JATS XML, Springer sidecar, DOI, and evidence.

    ``provenance`` must be the JATS source-provenance mapping returned by
    :func:`adapt_jats_bundle`. The accepted source status is supplied by the
    caller because the publisher's durable sidecar does not store that runtime
    result field. No network or credential access occurs.
    """
    validated, _, _, _, _ = _validate_provenance(provenance)
    return validated


def _read_sidecar_seed(xml_path: Path, expected_doi: str, source_status: str) -> dict[str, Any]:
    try:
        # Check the caller's original path before resolving it. Resolving first
        # would erase whether that path was a symlink to an otherwise valid file.
        if xml_path.is_symlink() or not xml_path.is_file():
            _fail("JATS_XML_MISSING_OR_UNSAFE")
        resolved_xml = xml_path.resolve(strict=True)
    except JatsAdapterError:
        raise
    except OSError:
        _fail("JATS_XML_MISSING_OR_UNSAFE")
    sidecar_path = Path(str(resolved_xml) + ".provenance.json")
    sidecar = _read_sidecar(sidecar_path)
    return {
        "resource_type": "jats_xml",
        "source": sidecar.get("source"),
        "source_parser": "jats_adapter",
        "source_status": source_status,
        "source_xml": str(resolved_xml),
        "source_sidecar": str(sidecar_path.resolve()),
        "source_xml_sha256": sidecar.get("sha256"),
        "doi": expected_doi,
        "endpoint": sidecar.get("endpoint"),
        "permission_evidence": sidecar.get("permission_evidence"),
        "jats_product_evidence": sidecar.get("jats_product_evidence"),
        "source_pdf": None,
    }


def _xml_anchor(node: ET.Element, parents: Mapping[ET.Element, ET.Element]) -> str:
    parts: list[str] = []
    current = node
    while current in parents:
        parent = parents[current]
        siblings = [child for child in list(parent)
                    if _local_name(child.tag) == _local_name(current.tag)]
        index = next(i for i, child in enumerate(siblings, 1) if child is current)
        parts.append(f"{_local_name(current.tag)}[{index}]")
        current = parent
    parts.append(_local_name(current.tag))
    path = "/" + "/".join(reversed(parts))
    xml_id = next((value for key, value in node.attrib.items()
                   if _local_name(key) == "id" and value), None)
    return path + (f"#id={quote(xml_id, safe='-._:')}" if xml_id else "")


def _caption(node: ET.Element) -> str:
    caption = next((child for child in list(node) if _local_name(child.tag) == "caption"), None)
    return _element_text(caption) if caption is not None else ""


def _cell_text(node: ET.Element) -> str:
    return _element_text(node).replace("|", r"\|").replace("\n", "<br>")


def _table_markdown(node: ET.Element) -> str:
    table = next((item for item in node.iter() if _local_name(item.tag) == "table"), None)
    if table is None:
        return ""
    rows: list[tuple[list[str], bool]] = []
    for row in (item for item in table.iter() if _local_name(item.tag) == "tr"):
        cells = [child for child in list(row)
                 if _local_name(child.tag) in {"th", "td"}]
        if cells:
            rows.append(([_cell_text(cell) for cell in cells],
                         any(_local_name(cell.tag) == "th" for cell in cells)))
    if not rows:
        return ""
    header_index = next((i for i, (_, is_header) in enumerate(rows) if is_header), None)
    if header_index is None:
        column_count = max(len(cells) for cells, _ in rows)
        header = [""] * column_count
        data_rows = [cells for cells, _ in rows]
    else:
        header, _ = rows[header_index]
        column_count = max(len(header), *(len(cells) for cells, _ in rows))
        header = header + [""] * (column_count - len(header))
        data_rows = [cells for i, (cells, _) in enumerate(rows) if i != header_index]
    data_rows = [cells + [""] * (column_count - len(cells)) for cells in data_rows]
    lines = ["| " + " | ".join(header) + " |",
             "| " + " | ".join("---" for _ in range(column_count)) + " |"]
    lines.extend("| " + " | ".join(cells) + " |" for cells in data_rows)
    return "\n".join(lines)


def _serialized_xml(node: ET.Element) -> str:
    return ET.tostring(node, encoding="unicode", short_empty_elements=True)


def adapt_jats_bundle(
    xml_path: Path | str,
    expected_doi: str,
    *,
    source_status: str,
) -> dict[str, Any]:
    """Build a CanonicalDocument from one verified downloaded/exists JATS file."""
    normalized_doi = _normalize_doi(expected_doi)
    if source_status not in ALLOWED_SOURCE_STATUSES:
        _fail("JATS_SOURCE_STATUS_INVALID")
    path = Path(xml_path)
    seed = _read_sidecar_seed(path, normalized_doi, source_status)
    provenance, root, article, title, body = _validate_provenance(seed)
    document_id = (
        "springer-jats-" + hashlib.sha256(normalized_doi.encode("ascii")).hexdigest()[:16]
        + "-" + provenance["source_xml_sha256"][:12]
    )
    parents = {child: parent for parent in root.iter() for child in list(parent)}
    blocks: list[dict[str, Any]] = []
    sections: list[dict[str, Any]] = []
    section_stack: list[tuple[int, dict[str, Any]]] = []

    def add_block(
        node: ET.Element,
        source_type: str,
        block_type: str,
        text: str,
        *,
        heading_level: int | None = None,
        content_items: list[dict[str, Any]] | None = None,
        anchor_node: ET.Element | None = None,
        anchor_suffix: str | None = None,
    ) -> dict[str, Any]:
        anchor = _xml_anchor(anchor_node or node, parents)
        if anchor_suffix:
            anchor += f"::{anchor_suffix}"
        block = {
            "block_id": f"{document_id}:{anchor}",
            "page_index": None,
            "source_type": source_type,
            "type": block_type,
            "text": text.strip(),
            "heading_level": heading_level,
            "bbox": None,
            "order": len(blocks),
            "content_items": content_items or [],
            "asset_paths": [],
            "xml_anchor": anchor,
        }
        blocks.append(block)
        if block_type == "heading":
            while section_stack and section_stack[-1][0] >= (heading_level or 1):
                section_stack.pop()
            parent_section = section_stack[-1][1] if section_stack else None
            section = {
                "section_raw": text.strip(),
                "section_normalized": None,
                "heading_level": heading_level,
                "block_ids": [block["block_id"]],
                "page_start": None,
                "page_end": None,
                "xml_anchor": anchor,
                "parent_section_raw": parent_section.get("section_raw") if parent_section else None,
            }
            sections.append(section)
            section_stack.append((heading_level or 1, section))
        elif section_stack:
            section_stack[-1][1]["block_ids"].append(block["block_id"])
        return block

    title_node = next(node for node in article.iter()
                      if _local_name(node.tag) == "article-title" and _element_text(node))
    add_block(title_node, "doc_title", "title", title)

    def emit_figure(node: ET.Element) -> None:
        label_node = next((child for child in list(node) if _local_name(child.tag) == "label"), None)
        label = _element_text(label_node) if label_node is not None else ""
        caption_text = _caption(node)
        caption_text = " ".join(part for part in (label, caption_text) if part)
        if not caption_text:
            return
        add_block(
            node, "image", "figure_caption", caption_text,
            content_items=[{"type": "image_caption", "content": caption_text}],
        )

    def emit_table(node: ET.Element) -> None:
        label_node = next((child for child in list(node) if _local_name(child.tag) == "label"), None)
        label = _element_text(label_node) if label_node is not None else ""
        caption_text = " ".join(part for part in (label, _caption(node)) if part)
        table_text = _table_markdown(node)
        if not caption_text and not table_text:
            return
        text_parts = []
        if caption_text:
            text_parts.append(caption_text)
        if table_text:
            text_parts.append(table_text)
        content_items = []
        if caption_text:
            content_items.append({"type": "table_caption", "content": caption_text})
        if table_text:
            content_items.append({"type": "table_body", "content": table_text})
        add_block(node, "table", "table", "\n\n".join(text_parts), content_items=content_items)

    def emit_formula(node: ET.Element) -> None:
        serialized = _serialized_xml(node)
        text = "Formula source XML:\n```xml\n" + serialized + "\n```"
        add_block(node, "equation", "equation", text,
                  content_items=[{"type": "formula_xml", "content": serialized}])

    def paragraph_segments(node: ET.Element) -> list[tuple[str, str | ET.Element]]:
        """Split inline formula nodes from surrounding readable paragraph text."""
        segments: list[tuple[str, str | ET.Element]] = []
        text_buffer: list[str] = []

        def flush_text() -> None:
            text = " ".join("".join(text_buffer).split())
            if text:
                segments.append(("text", text))
            text_buffer.clear()

        def walk(current: ET.Element) -> None:
            if current.text:
                text_buffer.append(current.text)
            for child in list(current):
                if _local_name(child.tag) == "inline-formula":
                    flush_text()
                    segments.append(("formula", child))
                else:
                    walk(child)
                if child.tail:
                    text_buffer.append(child.tail)

        walk(node)
        flush_text()
        return segments

    def process_element(node: ET.Element, level: int) -> None:
        name = _local_name(node.tag)
        if name == "sec":
            process_section(node, level)
        elif name == "p":
            segments = paragraph_segments(node)
            has_formula = any(kind == "formula" for kind, _ in segments)
            text_index = 0
            for kind, value in segments:
                if kind == "formula" and isinstance(value, ET.Element):
                    emit_formula(value)
                elif kind == "text" and isinstance(value, str):
                    suffix = f"text-segment[{text_index + 1}]" if has_formula else None
                    add_block(node, "text", "paragraph", value,
                              anchor_suffix=suffix)
                    text_index += 1
        elif name == "fig":
            emit_figure(node)
        elif name == "table-wrap":
            emit_table(node)
        elif name in {"disp-formula", "disp-formula-group", "inline-formula"}:
            formula_nodes = ([child for child in list(node)
                              if _local_name(child.tag) in {"disp-formula", "inline-formula"}]
                             if name == "disp-formula-group" else [node])
            if not formula_nodes:
                formula_nodes = [node]
            for formula in formula_nodes:
                emit_formula(formula)
        elif name == "list":
            for item in (child for child in node.iter() if _local_name(child.tag) == "list-item"):
                text = _element_text(item)
                if text:
                    add_block(item, "list_item", "list", text)
        elif name in {"ref-list", "title", "label", "table", "caption"}:
            return
        else:
            for child in list(node):
                process_element(child, level)

    def process_section(node: ET.Element, level: int) -> None:
        children = list(node)
        label_node = next((child for child in children if _local_name(child.tag) == "label"), None)
        title_node = next((child for child in children if _local_name(child.tag) == "title"), None)
        section_title = _element_text(title_node) if title_node is not None else ""
        label = _element_text(label_node) if label_node is not None else ""
        heading = " ".join(part for part in (label, section_title) if part)
        if heading:
            add_block(node, "paragraph_title", "heading", heading, heading_level=level)
        for child in children:
            if child is label_node or child is title_node:
                continue
            process_element(child, level + 1 if _local_name(child.tag) == "sec" else level)

    for child in list(body):
        process_element(child, 2 if _local_name(child.tag) == "sec" else 2)

    # A ref-list is a JATS structural reference section. Its heading is kept in
    # CanonicalDocument so the existing chunker can exclude it by default.
    for ref_list in (node for node in article.iter() if _local_name(node.tag) == "ref-list"):
        title_node = next((child for child in list(ref_list) if _local_name(child.tag) == "title"), None)
        heading = _element_text(title_node) if title_node is not None else "References"
        add_block(ref_list, "paragraph_title", "heading", heading, heading_level=2)
        for ref in (node for node in ref_list.iter() if _local_name(node.tag) == "ref"):
            text = _element_text(ref)
            if text:
                add_block(ref, "ref_text", "reference", text)

    return {
        "document_id": document_id,
        "title": title,
        "provenance": provenance,
        "pages": [],
        "blocks": blocks,
        "sections": sections,
    }


def chunk_jats_document(
    document: Mapping[str, Any],
    *,
    include_references: bool = False,
    target_tokens: int = 600,
    max_tokens: int = 900,
) -> list[dict[str, Any]]:
    """Run the repository's structure-aware chunker and attach JATS trace data.

    ``mineru_block_ids`` remains populated because it is the existing chunk
    schema field consumed downstream. Those values are JATS adapter block IDs;
    ``jats_block_ids`` and ``xml_anchors`` are the truthful source-specific
    aliases. ``source_parser`` always identifies this adapter, never MinerU.
    """
    if document.get("provenance", {}).get("source_parser") != "jats_adapter":
        _fail("JATS_CANONICAL_SOURCE_INVALID")
    if (type(target_tokens) is not int or type(max_tokens) is not int
            or not 1 <= target_tokens <= max_tokens <= 20000):
        _fail("CHUNK_OPTIONS_INVALID")
    blocks_by_id = {str(block["block_id"]): block for block in document.get("blocks", [])}
    chunks = chunk_document(
        dict(document), target_tokens=target_tokens, max_tokens=max_tokens,
        include_references=include_references, include_front_matter=True,
    )
    source_provenance = document["provenance"]
    for chunk in chunks:
        block_ids = [str(value) for value in chunk.get("mineru_block_ids", [])]
        chunk["jats_block_ids"] = block_ids
        chunk["xml_anchors"] = list(dict.fromkeys(
            blocks_by_id[block_id]["xml_anchor"]
            for block_id in block_ids if block_id in blocks_by_id
        ))
        for location in chunk.get("locations", []):
            block = blocks_by_id.get(str(location.get("block_id")))
            if block is not None:
                location["xml_anchor"] = block["xml_anchor"]
                location["page_index"] = None
                location["bbox"] = None
        chunk["page_start"] = None
        chunk["page_end"] = None
        chunk["provenance"].update({
            "resource_type": "jats_xml",
            "source_parser": "jats_adapter",
            "source_status": source_provenance["source_status"],
            "source": source_provenance["source"],
            "source_xml": source_provenance["source_xml"],
            "source_sidecar": source_provenance["source_sidecar"],
            "source_xml_sha256": source_provenance["source_xml_sha256"],
            "doi": source_provenance["doi"],
            "endpoint": source_provenance["endpoint"],
            "permission_evidence": source_provenance["permission_evidence"],
            "jats_product_evidence": source_provenance["jats_product_evidence"],
            "source_pdf": None,
            "source_pdf_sha256": None,
            "parser_schema": "jats",
            "parser_schema_version": source_provenance.get("parser_schema_version"),
        })
    return chunks


def render_markdown(document: Mapping[str, Any]) -> str:
    """Render article title and extracted XML blocks with traceable anchors."""
    title = str(document.get("title") or "Untitled")
    provenance = document.get("provenance", {})
    lines = [f"# {title}", "",
             f"> DOI: `{provenance.get('doi', '')}`",
             f"> Source: JATS XML · SHA-256 `{provenance.get('source_xml_sha256', '')}`", ""]
    for block in sorted(document.get("blocks", []), key=lambda item: item.get("order", 0)):
        block_type = block.get("type")
        text = str(block.get("text") or "").strip()
        if block_type == "title" or not text:
            continue
        anchor = str(block.get("xml_anchor") or "")
        lines.append(f"> JATS XML element: `{anchor.replace('`', '')}`")
        if block_type == "heading":
            level = block.get("heading_level")
            hashes = "#" * max(2, min(6, level if isinstance(level, int) else 2))
            lines.extend([f"{hashes} {text}", ""])
        elif block.get("source_type") == "image":
            lines.extend([f"**Figure caption:** {text}", ""])
        elif block.get("source_type") == "ref_text":
            lines.extend([f"- {text}", ""])
        else:
            lines.extend([text, ""])
    return "\n".join(lines).rstrip() + "\n"


def _jsonl_bytes(chunks: Sequence[Mapping[str, Any]]) -> bytes:
    return "".join(json.dumps(chunk, ensure_ascii=False, separators=(",", ":")) + "\n"
                   for chunk in chunks).encode("utf-8")


def _output_paths(output_dir: Path, document: Mapping[str, Any]) -> dict[str, Path]:
    document_id = str(document["document_id"])
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", document_id):
        _fail("OUTPUT_DOCUMENT_ID_INVALID")
    return {
        "jsonl": output_dir / f"{document_id}.jsonl",
        "markdown": output_dir / f"{document_id}.md",
    }


def write_outputs(
    output_dir: Path | str,
    document: Mapping[str, Any],
    chunks: Sequence[Mapping[str, Any]],
    markdown: str,
) -> dict[str, Any]:
    """Create JSONL/Markdown once, or verify byte-identical existing outputs."""
    try:
        directory = Path(output_dir)
    except (TypeError, ValueError):
        _fail("OUTPUT_DIRECTORY_INVALID")
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        _fail("OUTPUT_DIRECTORY_INVALID")
    paths = _output_paths(directory, document)
    payloads = {"jsonl": _jsonl_bytes(chunks), "markdown": markdown.encode("utf-8")}
    # Preflight every existing artifact before creating any missing peer.
    for kind, path in paths.items():
        if path.is_symlink():
            _fail("OUTPUT_CONFLICT")
        if path.exists():
            try:
                if not path.is_file() or path.read_bytes() != payloads[kind]:
                    _fail("OUTPUT_CONFLICT")
            except OSError:
                _fail("OUTPUT_CONFLICT")
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        _fail("OUTPUT_DIRECTORY_CREATE_FAILED")
    created: dict[str, bool] = {}
    for kind, path in paths.items():
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                if path.is_symlink() or not path.is_file() or path.read_bytes() != payloads[kind]:
                    _fail("OUTPUT_CONFLICT")
                created[kind] = False
                continue
            except OSError:
                _fail("OUTPUT_CONFLICT")
        except OSError:
            _fail("OUTPUT_WRITE_FAILED")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payloads[kind])
        except OSError:
            try:
                path.unlink()
            except OSError:
                pass
            _fail("OUTPUT_WRITE_FAILED")
        created[kind] = True
    return {
        "status": "created" if any(created.values()) else "verified_existing",
        "outputs": {key: str(path.resolve()) for key, path in paths.items()},
        "created": created,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xml_path", type=Path)
    parser.add_argument("--doi", required=True, help="Expected article DOI")
    parser.add_argument("--source-status", required=True, choices=sorted(ALLOWED_SOURCE_STATUSES))
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--execute", action="store_true", help="Create outputs if absent")
    parser.add_argument("--include-references", action="store_true")
    parser.add_argument("--target-tokens", type=int, default=600)
    parser.add_argument("--max-tokens", type=int, default=900)
    args = parser.parse_args(argv)
    try:
        document = adapt_jats_bundle(
            args.xml_path, args.doi, source_status=args.source_status
        )
        chunks = chunk_jats_document(
            document,
            include_references=args.include_references,
            target_tokens=args.target_tokens,
            max_tokens=args.max_tokens,
        )
        markdown = render_markdown(document)
        planned = _output_paths(args.output_dir, document)
        if args.execute:
            result = write_outputs(args.output_dir, document, chunks, markdown)
            status = result["status"]
            outputs = result["outputs"]
        else:
            status = "preview"
            outputs = {key: str(path) for key, path in planned.items()}
        print(json.dumps({
            "status": status,
            "resource_type": "jats_xml",
            "source_parser": "jats_adapter",
            "document_id": document["document_id"],
            "doi": document["provenance"]["doi"],
            "source_xml_sha256": document["provenance"]["source_xml_sha256"],
            "chunks": len(chunks),
            "references_indexed": args.include_references,
            "outputs": outputs,
        }, ensure_ascii=False))
        return 0
    except JatsAdapterError as failure:
        print(json.dumps({"status": "error", "code": failure.code}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    except ValueError:
        print(json.dumps({"status": "error", "code": "CLI_VALUE_INVALID"}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    except OSError:
        print(json.dumps({"status": "error", "code": "LOCAL_IO_FAILED"}, ensure_ascii=False),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
