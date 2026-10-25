"""Pure source-metadata helpers suitable for inlining in a Dify Code node.

Validation is syntactic only. Dates retain their source text; neither paper
identity nor publication-date accuracy is verified here. No I/O is performed.
"""

import json
import re
import unicodedata


_SOURCE_METADATA_FIELDS = (
    ("publication_date", 64, "发表日期（来源原文，未规范化）"),
    ("publication_date_source", 128, "发表日期来源"),
    ("source_database", 128, "来源数据库"),
    ("source_id", 64, "来源ID"),
)
_SOURCE_METADATA_ERROR = "invalid source metadata"
_SOURCE_METADATA_UNSAFE = re.compile(
    r"[?&=]|[a-z][a-z0-9+.-]*://|\b(?:https?|ftp|file|data|javascript):|"
    r"//|\bwww\.|\b(?:[a-z0-9-]+\.)+[a-z]{2,63}\b|"
    r"\b(?:q|query|term|search)\s*:|"
    r"\b(?:bearer|basic)\s+|"
    r"\b(?:[a-z0-9]+[_-])*(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|token|"
    r"password|passwd|secret|authorization|cookie)\s*[:=]|"
    r"\b(?:sk-[a-z0-9_-]+|gh[pousr]_[a-z0-9_]+|github_pat_[a-z0-9_]+|"
    r"(?:AKIA|ASIA)[A-Z0-9]{16})\b|"
    r"\beyJ[a-z0-9_-]*\.[a-z0-9_-]+\.[a-z0-9_-]+\b|"
    r"-----BEGIN\b",
    re.IGNORECASE,
)


def _source_metadata_invalid():
    raise ValueError(_SOURCE_METADATA_ERROR) from None


def normalize_source_metadata(result) -> dict:
    """Return exactly four optional fields, ignoring other retrieval fields.

    Missing fields and explicit None remain None. Supplied values must be
    bounded, nonblank strings without control characters, URLs, query syntax,
    or recognizable credential formats. Text is preserved without date parsing.
    An explicit PubMed ID must contain 1–16 ASCII digits; this does not verify
    its association with a paper. Every rejection uses a fixed, redacted error.
    """
    if type(result) is not dict:
        _source_metadata_invalid()
    metadata = {}
    for key, limit, _ in _SOURCE_METADATA_FIELDS:
        value = result.get(key)
        if value is not None:
            if type(value) is not str or not 1 <= len(value) <= limit:
                _source_metadata_invalid()
            if not value.strip() or not any(char.isalnum() for char in value):
                _source_metadata_invalid()
            if any(unicodedata.category(char) in ("Cc", "Cf", "Cs", "Zl", "Zp")
                   for char in value):
                _source_metadata_invalid()
            if _SOURCE_METADATA_UNSAFE.search(value):
                _source_metadata_invalid()
        metadata[key] = value
    if metadata["publication_date_source"] is not None and metadata["publication_date"] is None:
        _source_metadata_invalid()
    database = metadata["source_database"]
    source_id = metadata["source_id"]
    if database is not None and database.strip().casefold() == "pubmed" and source_id is not None:
        if re.fullmatch(r"[0-9]{1,16}", source_id) is None:
            _source_metadata_invalid()
    return metadata


def source_metadata_lines(metadata) -> list[str]:
    """Render validated values as four labeled JSON strings, including unknowns."""
    normalized = normalize_source_metadata(metadata)
    return [label + "：" + json.dumps(
        normalized[key] if normalized[key] is not None else "检索结果未提供",
        ensure_ascii=False,
    ) for key, _, label in _SOURCE_METADATA_FIELDS]


def normalize_docx_source(result):
    """Validate restored DOCX identity and native block locators, without I/O.

    The trusted import layer verifies original bytes and parent binding. This
    gate checks the public trace structure; it does not verify scientific truth.
    """
    if type(result) is not dict:
        raise ValueError("invalid DOCX trace")
    sha = result.get("source_docx_sha256")
    xml_sha = result.get("source_document_xml_sha256")
    parent = result.get("parent_document_id")
    if (result.get("resource_type") != "docx_ooxml"
            or result.get("source_parser") != "docx_methods_source"
            or type(sha) is not str or re.fullmatch(r"[0-9a-f]{64}", sha) is None
            or type(xml_sha) is not str or re.fullmatch(r"[0-9a-f]{64}", xml_sha) is None
            or result.get("document_id") != sha[:20]
            or type(parent) is not str or re.fullmatch(r"[0-9a-f]{20}", parent) is None
            or parent == result["document_id"]
            or result.get("source_role") != "supporting_information"
            or any(result.get(k) is not None for k in ("source_pdf_sha256", "page_start", "page_end"))):
        raise ValueError("invalid DOCX source")
    ids, paths = result.get("docx_block_ids"), result.get("docx_xml_paths")
    if (type(ids) is not list or not ids or ids != result.get("mineru_block_ids")
            or type(paths) is not list or len(paths) != len(ids)):
        raise ValueError("invalid DOCX blocks")
    pairs = []
    for block, path in zip(ids, paths):
        match = re.fullmatch(re.escape(sha[:20]) + r":B([0-9]{4,})", str(block))
        if (match is None or int(match[1]) < 1
                or path != "/w:document/w:body/*[" + str(int(match[1])) + "]"):
            raise ValueError("invalid DOCX location")
        pairs.append((block, path))
    if len(set(pairs)) != len(pairs):
        raise ValueError("duplicate DOCX location")
    return {key: result[key] for key in ("resource_type", "source_parser",
        "source_docx_sha256", "source_document_xml_sha256", "source_role",
        "parent_document_id", "docx_block_ids", "docx_xml_paths")}
