"""Verify a manually reviewed local DOCX methods selection, without downloading.

The CanonicalDocument is a reviewed selection map, not an independent text
source. Every selected paragraph is rebuilt from the hash-bound original DOCX.
The reviewed methods map supports native paragraphs and sup/sub typography.
An explicit extension includes subsequent native captions and simple tables;
images are inventoried but not interpreted. Tracked changes, merged tables and
OMML fail closed. No physical pages are inferred.
"""

import copy
import hashlib
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from structure_aware_chunker import chunk_document, load_local_token_counter


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
PARSER = "docx_methods_source"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _require(condition, reason):
    if not condition:
        raise ValueError("DOCX_" + reason)


def _paragraph_text(paragraph, *, allow_images=False):
    _require(paragraph.tag == W + "p", "NON_PARAGRAPH_SELECTION")
    if allow_images:
        paragraph = copy.deepcopy(paragraph)
        for parent in paragraph.iter():
            for child in list(parent):
                if child.tag in (W + "drawing", W + "pict"):
                    parent.remove(child)
    _require(not any(n.tag in (W + "ins", W + "del", W + "moveFrom", W + "moveTo",
                               M + "oMath", M + "oMathPara", W + "drawing", W + "object",
                               W + "fldChar", W + "instrText", W + "pict")
                     for n in paragraph.iter()), "UNSUPPORTED_PARAGRAPH_CONTENT")
    output = []
    for run in paragraph.iter(W + "r"):
        text = "".join((n.text or "") if n.tag == W + "t" else
                       "\t" if n.tag == W + "tab" else "\n"
                       for n in run.iter() if n.tag in (W + "t", W + "tab", W + "br", W + "cr"))
        align = run.find(W + "rPr/" + W + "vertAlign")
        value = align.get(W + "val") if align is not None else None
        if value in ("superscript", "subscript") and text:
            tag = "sup" if value == "superscript" else "sub"
            text = "<" + tag + ">" + text + "</" + tag + ">"
        output.append(text)
    return "".join(output)


def _table_text(table):
    """Keep native row/column boundaries; unsupported merged cells fail closed."""
    _require(table.tag == W + "tbl", "NOT_A_TABLE")
    _require(not any(n.tag in (W + "vMerge", W + "hMerge", W + "gridSpan",
                               W + "ins", W + "del", W + "tbl")
                     for n in table.iter() if n is not table), "UNSUPPORTED_TABLE_STRUCTURE")
    rows = []
    width = None
    for row in table.findall(W + "tr"):
        cells = row.findall(W + "tc")
        width = len(cells) if width is None else width
        _require(width > 0 and len(cells) == width, "UNEVEN_TABLE")
        texts = ["<br>".join(_paragraph_text(p).replace("|", "&#124;")
                            .replace("\n", "<br>") for p in cell.findall(W + "p"))
                 for cell in cells]
        rows.append("| " + " | ".join(texts) + " |")
    _require(bool(rows), "EMPTY_TABLE")
    # The first source row remains the header, with no inferred units or values.
    return "\n".join([rows[0], "| " + " | ".join(["---"] * width) + " |", *rows[1:]])


def _supporting_tail(original, source_id, after):
    blocks, images = [], []
    for index in range(after + 1, len(original) + 1):
        node = original[index - 1]
        if node.tag == W + "p":
            text = _paragraph_text(node, allow_images=True)
            if text.strip().casefold() == "references":
                break
            if any(n.tag in (W + "drawing", W + "pict") for n in node.iter()):
                images.append("B" + str(index).zfill(4))
            if not text.strip():
                continue
            heading = text.strip().casefold() == "supplementary figures"
            kind, source_type = ("heading", "docx_heading") if heading else ("paragraph", "docx_paragraph")
        elif node.tag == W + "tbl":
            text, heading, kind, source_type = _table_text(node), False, "table", "table"
        elif node.tag in (W + "bookmarkStart", W + "bookmarkEnd", W + "sectPr"):
            continue
        else:
            raise ValueError("DOCX_UNSUPPORTED_BODY_CONTENT")
        label = "B" + str(index).zfill(4)
        blocks.append({"block_id": source_id + ":" + label, "source_docx_block": label,
                       "source_xml_path": "/w:document/w:body/*[" + str(index) + "]",
                       "type": kind, "source_type": source_type, "text": text,
                       "page_index": None, "bbox": None, "order": index,
                       "heading_level": 1 if heading else None})
    return blocks, images


def extend_supporting_document(methods_path):
    """Extend an already reviewed complete methods map with native captions/tables.

    Images are inventoried, never OCR'd or interpreted. Original methods headings
    and source binding are reused. The returned canonical document is local data;
    this function does not write a file or call a service.
    """
    document = copy.deepcopy(verified_document(methods_path))
    _require(document["provenance"].get("selection_scope") is None, "ALREADY_EXTENDED")
    with zipfile.ZipFile(document["provenance"]["source_docx"]) as archive:
        original = list(ET.fromstring(archive.read("word/document.xml")).find(W + "body"))
    after = document["blocks"][-1]["order"]
    blocks, images = _supporting_tail(original, document["document_id"], after)
    document["blocks"].extend(blocks)
    document["provenance"].update({"selection_scope": "methods_captions_native_tables",
        "methods_end_body_index": after, "unparsed_image_body_blocks": images,
        "selection": "complete reviewed methods plus subsequent native captions and tables; images and references excluded"})
    return document


def native_supporting_document(source_docx, binding_path, *, zotero_parent_key, zotero_attachment_key):
    binding = json.loads(Path(binding_path).read_text(encoding="utf-8"))
    _require(sha256(source_docx) == binding["source_sha256"], "ORIGINAL_CHANGED")
    with zipfile.ZipFile(source_docx) as archive:
        _require(archive.getinfo("word/document.xml").file_size <= 16 * 1024 * 1024,
                 "DOCUMENT_XML_TOO_LARGE")
        xml = archive.read("word/document.xml")
    _require(b"<!DOCTYPE" not in xml and b"<!ENTITY" not in xml, "XML_DECLARATIONS_UNSUPPORTED")
    body = ET.fromstring(xml).find(W + "body")
    _require(body is not None, "BODY_MISSING")
    source_id = binding["source_sha256"][:20]
    blocks, images = _supporting_tail(list(body), source_id, 0)
    provenance = {"resource_type": "docx_ooxml", "source_role": "supporting_information",
        "source_docx": str(Path(source_docx).resolve()), "source_docx_sha256": binding["source_sha256"],
        "source_document_xml_sha256": hashlib.sha256(xml).hexdigest(),
        "source_binding": str(Path(binding_path).resolve()), "source_binding_sha256": sha256(binding_path),
        "parent_document_id": binding["parent_pdf_project_document_id"],
        "source_url": binding["source_url"], "doi": binding["doi"],
        "pmcid": binding.get("pmcid", ""), "pmid": binding.get("pmid", ""),
        "physical_pages_verified": False, "selection_scope": "native_supporting_document",
        "unparsed_image_body_blocks": images, "zotero_parent_key": zotero_parent_key,
        "zotero_attachment_key": zotero_attachment_key,
        "attachment_name": Path(source_docx).name}
    return {"document_id": source_id, "title": binding["source_title"] + " [Supporting Information]",
            "pages": [], "blocks": blocks, "provenance": provenance}


def verified_document(canonical_path):
    """Check a reviewed paragraph map against its original file and binding."""
    document = json.loads(Path(canonical_path).read_text(encoding="utf-8"))
    _require(isinstance(document, dict), "CANONICAL_INVALID")
    provenance = document.get("provenance")
    _require(isinstance(provenance, dict), "PROVENANCE_INVALID")
    for field in ("source_docx_sha256", "source_document_xml_sha256", "source_binding_sha256"):
        _require(isinstance(provenance.get(field), str) and SHA256.fullmatch(provenance[field]),
                 "SOURCE_HASH_INVALID")
    _require(provenance.get("resource_type") == "docx_ooxml"
             and provenance.get("source_role") == "supporting_information"
             and provenance.get("physical_pages_verified") is False
             and document.get("pages") == [], "SOURCE_KIND_INVALID")
    _require(not any(provenance.get(k) is not None for k in
                     ("source_pdf", "source_pdf_sha256", "sourcePdf", "sourcePdfSha256")),
             "CLAIMS_PDF")
    _require(sha256(provenance["source_binding"]) == provenance["source_binding_sha256"],
             "BINDING_CHANGED")
    binding = json.loads(Path(provenance["source_binding"]).read_text(encoding="utf-8"))
    _require(isinstance(binding, dict), "BINDING_INVALID")
    for field, binding_field in (
            ("source_docx_sha256", "source_sha256"),
            ("source_document_xml_sha256", "word_document_xml_sha256"),
            ("parent_document_id", "parent_pdf_project_document_id"),
            ("source_role", "source_role"), ("source_url", "source_url"),
            ("doi", "doi"), ("pmcid", "pmcid"), ("pmid", "pmid")):
        optional = field in ("pmcid", "pmid") and provenance.get("selection_scope") == "native_supporting_document"
        _require(isinstance(provenance.get(field), str) and (provenance[field] or optional)
                 and provenance[field] == binding.get(binding_field, ""), "PARENT_BINDING_DIFFERS")
    source_id = provenance["source_docx_sha256"][:20]
    _require(document.get("document_id") == binding.get("supplement_source_id") == source_id
             and document.get("title") == binding.get("source_title", "") + " [Supporting Information]",
             "DOCUMENT_IDENTITY_DIFFERS")
    _require(sha256(provenance["source_docx"]) == provenance["source_docx_sha256"], "ORIGINAL_CHANGED")
    try:
        with zipfile.ZipFile(provenance["source_docx"]) as archive:
            _require(not any(n.lower().endswith("vbaproject.bin") for n in archive.namelist()), "MACROS")
            info = archive.getinfo("word/document.xml")
            _require(info.file_size <= 16 * 1024 * 1024, "DOCUMENT_XML_TOO_LARGE")
            xml = archive.read(info)
    except zipfile.BadZipFile:
        raise ValueError("DOCX_ARCHIVE_INVALID") from None
    _require(hashlib.sha256(xml).hexdigest() == provenance["source_document_xml_sha256"], "XML_CHANGED")
    _require(b"<!DOCTYPE" not in xml and b"<!ENTITY" not in xml, "XML_DECLARATIONS_UNSUPPORTED")
    try:
        body = ET.fromstring(xml).find(W + "body")
    except ET.ParseError:
        raise ValueError("DOCX_XML_INVALID") from None
    _require(body is not None, "BODY_MISSING")
    original = list(body)
    blocks = document.get("blocks")
    _require(isinstance(blocks, list) and bool(blocks), "SELECTION_EMPTY")
    native = provenance.get("selection_scope") == "native_supporting_document"
    if native:
        expected, images = _supporting_tail(original, source_id, 0)
        _require(blocks == expected and provenance.get("unparsed_image_body_blocks") == images,
                 "NATIVE_SELECTION_DIFFERS")
        return document
    supporting = provenance.get("selection_scope") == "methods_captions_native_tables"
    if supporting:
        after = provenance.get("methods_end_body_index")
        _require(type(after) is int and 1 <= after <= len(original), "METHODS_END_INVALID")
        expected_tail, images = _supporting_tail(original, source_id, after)
        _require([b for b in blocks if b.get("order", 0) > after] == expected_tail
                 and provenance.get("unparsed_image_body_blocks") == images,
                 "SUPPORTING_SELECTION_DIFFERS")
    indexes = []
    for block in blocks:
        _require(isinstance(block, dict), "BLOCK_INVALID")
        _require(set(block) == {"block_id", "source_docx_block", "source_xml_path", "type",
                              "source_type", "text", "page_index", "bbox", "order", "heading_level"},
                 "BLOCK_FIELDS_UNSUPPORTED")
        index = block.get("order")
        _require(type(index) is int and 1 <= index <= len(original), "BLOCK_ORDER_INVALID")
        indexes.append(index)
        label = "B" + str(index).zfill(4)
        _require(block.get("block_id") == source_id + ":" + label
                 and block.get("source_docx_block") == label
                 and block.get("source_xml_path") == "/w:document/w:body/*[" + str(index) + "]",
                 "BLOCK_LOCATION_DIFFERS")
        _require(block.get("page_index") is None and block.get("bbox") is None, "CLAIMS_PAGE")
        heading = block.get("type") == "heading"
        table = supporting and block.get("type") == "table"
        _require(block.get("type") in (("heading", "paragraph", "table") if supporting else ("heading", "paragraph"))
                 and block.get("source_type") == ("table" if table else "docx_heading" if heading else "docx_paragraph")
                 and (type(block.get("heading_level")) is int and block["heading_level"] in (1, 2) if heading else
                      block.get("heading_level") is None), "BLOCK_KIND_INVALID")
        text = (_table_text(original[index - 1]) if table else
                _paragraph_text(original[index - 1], allow_images=supporting and index > after))
        _require(block.get("text") == text, "TEXT_DIFFERS_FROM_ORIGINAL")
    _require(indexes == sorted(set(indexes)), "BLOCK_ORDER_DIFFERS")
    _require(blocks[0].get("type") == "heading"
             and blocks[0]["text"].strip().lower() == "materials and methods", "METHODS_HEADING_MISSING")
    # A manual range may omit blank paragraphs, never substantive content.
    for index in range(indexes[0], indexes[-1] + 1):
        if supporting and index > after:
            # Entire tail was regenerated and compared above, including blanks/images.
            continue
        if index not in indexes:
            node = original[index - 1]
            if node.tag in (W + "bookmarkStart", W + "bookmarkEnd"):
                _require(not list(node) and not (node.text or "").strip(), "BOOKMARK_HAS_CONTENT")
                continue
            _require(not _paragraph_text(node).strip(), "PARAGRAPH_OMITTED")
    return document


def chunk_docx_methods(canonical_path, tokenizer_path, *, target_tokens=600, max_tokens=900):
    """Rebuild complete methods chunks with the existing, locally pinned tokenizer."""
    _require(type(target_tokens) is int and type(max_tokens) is int
             and 1 <= target_tokens <= max_tokens <= 20000, "TOKEN_POLICY_INVALID")
    document = verified_document(canonical_path)
    counter = load_local_token_counter(Path(tokenizer_path))
    provenance = {**document["provenance"], "source_parser": PARSER,
                  "parser_schema": PARSER, "parser_schema_version": "1",
                  "source_canonical": str(Path(canonical_path).resolve()),
                  "source_canonical_sha256": sha256(canonical_path),
                  "tokenizer_json": str(Path(tokenizer_path).resolve()),
                  "tokenizer_sha256": sha256(tokenizer_path)}
    chunks = chunk_document(document, target_tokens=target_tokens, max_tokens=max_tokens,
                            include_front_matter=document["provenance"].get("selection_scope") == "native_supporting_document",
                            token_counter=counter)
    blocks = {b["block_id"]: b for b in document["blocks"]}
    for chunk in chunks:
        _require((document["provenance"].get("selection_scope") in ("methods_captions_native_tables", "native_supporting_document")
                  or chunk["section"] == "METHODS") and not chunk["oversize_atomic_unit"],
                 "METHOD_CHUNK_INVALID")
        chunk["provenance"] = copy.deepcopy(provenance)
        chunk["resource_type"] = "docx_ooxml"
        # Existing chunk IDs use this legacy key; explicit DOCX keys disambiguate it.
        chunk["docx_block_ids"] = list(chunk["mineru_block_ids"])
        chunk["docx_xml_paths"] = [blocks[k]["source_xml_path"] for k in chunk["docx_block_ids"]]
        for location in chunk["locations"]:
            location["docx_xml_path"] = blocks[location["block_id"]]["source_xml_path"]
    _require(bool(chunks), "CHUNKS_EMPTY")
    return chunks


def validate_docx_rows(rows):
    """Rebuild the full selection; reject changed text, trace, policy or omissions."""
    _require(isinstance(rows, list) and bool(rows), "CHUNKS_EMPTY")
    provenance = rows[0].get("provenance", {})
    _require(provenance.get("source_parser") == PARSER, "PARSER_INVALID")
    _require(sha256(provenance["source_canonical"]) == provenance["source_canonical_sha256"],
             "CANONICAL_CHANGED")
    _require(sha256(provenance["tokenizer_json"]) == provenance["tokenizer_sha256"], "TOKENIZER_CHANGED")
    expected = chunk_docx_methods(provenance["source_canonical"], provenance["tokenizer_json"],
                                 target_tokens=rows[0].get("target_tokens"),
                                 max_tokens=rows[0].get("max_tokens"))
    _require(len(rows) == len(expected), "CHUNK_SELECTION_INCOMPLETE")
    wanted = {row["chunk_id"]: row for row in expected}
    _require(len({row.get("chunk_id") for row in rows}) == len(rows), "CHUNK_IDS_DUPLICATED")
    for row in rows:
        _require(row == wanted.get(row.get("chunk_id")), "CHUNK_DIFFERS_FROM_ORIGINAL")
    return provenance


def validate_docx_result_trace(result):
    """Check restored source identity and XML locations; no file or network I/O."""
    source_hash = result.get("source_docx_sha256")
    _require(result.get("resource_type") == "docx_ooxml" and result.get("source_parser") == PARSER
             and isinstance(source_hash, str) and SHA256.fullmatch(source_hash)
             and result.get("document_id") == source_hash[:20]
             and isinstance(result.get("source_document_xml_sha256"), str)
             and SHA256.fullmatch(result["source_document_xml_sha256"])
             and result.get("source_role") == "supporting_information"
             and bool(result.get("parent_document_id"))
             and result.get("source_pdf_sha256") is None
             and result.get("page_start") is None and result.get("page_end") is None,
             "RETRIEVAL_SOURCE_INVALID")
    ids, paths, locations = (result.get(k) for k in ("docx_block_ids", "docx_xml_paths", "locations"))
    _require(isinstance(ids, list) and bool(ids) and ids == result.get("mineru_block_ids")
             and isinstance(paths, list) and len(paths) == len(ids)
             and isinstance(locations, list) and bool(locations), "RETRIEVAL_TRACE_INVALID")
    for block_id, path in zip(ids, paths):
        match = re.fullmatch(re.escape(source_hash[:20]) + r":B([0-9]{4,})", str(block_id))
        _require(match is not None and int(match[1]) > 0
                 and path == "/w:document/w:body/*[" + str(int(match[1])) + "]", "RETRIEVAL_PATH_INVALID")
    pairs = set(zip(ids, paths))
    _require(len(pairs) == len(ids), "RETRIEVAL_TRACE_DUPLICATED")
    _require({(loc.get("block_id"), loc.get("docx_xml_path")) for loc in locations
              if isinstance(loc, dict)} == pairs, "RETRIEVAL_LOCATIONS_DIFFER")
    for loc in locations:
        _require(isinstance(loc, dict) and loc.get("page_index") is None
                 and loc.get("bbox") is None, "RETRIEVAL_LOCATION_INVALID")
