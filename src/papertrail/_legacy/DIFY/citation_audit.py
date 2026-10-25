"""Dify Code-node-compatible final citation check for evidence-based answers.

This checks citation label membership and builds a deterministic source list.
It cannot establish whether the model's scientific interpretation is true.
"""

import json
import re

from evidence_source_metadata import normalize_source_metadata, normalize_docx_source


LABEL = re.compile(r"\[E[1-9][0-9]*\]")
NONCANONICAL_LABEL = re.compile(r"[\[［【(（]\s*[EＥ][0-9０-９]")
REFUSAL = "回答未通过引用核查，暂不能作为有来源的科研结论；请检查模型输出与检索证据。"


def _valid_manifest(raw):
    if not isinstance(raw, str) or len(raw) > 1000000:
        raise ValueError("invalid manifest size")
    data = json.loads(raw)
    if not isinstance(data, list) or not 1 <= len(data) <= 10:
        raise ValueError("invalid manifest")
    by_label = {}
    chunks = set()
    for index, item in enumerate(data, 1):
        if not isinstance(item, dict) or item.get("label") != "E" + str(index):
            raise ValueError("invalid label order")
        for key in ("chunk_id", "document_id", "title"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ValueError("missing source field")
        if item.get("resource_type") == "docx_ooxml":
            normalize_docx_source(item)
        elif item.get("resource_type") not in (None, "pdf") or not re.fullmatch(r"[0-9a-fA-F]{64}", str(item.get("source_pdf_sha256") or "")):
            raise ValueError("invalid source hash")
        for key in ("section", "subsection", "doi", "material_version", "correction_of", "parent_document_id", "attachment_name"):
            if item.get(key) is not None and not isinstance(item[key], str):
                raise ValueError("invalid source metadata")
        if item["chunk_id"] in chunks:
            raise ValueError("duplicate source chunk")
        chunks.add(item["chunk_id"])
        start, end = item.get("page_start"), item.get("page_end")
        if start is None and end is None:
            pass
        elif isinstance(start, bool) or not isinstance(start, int) or start < 0 or isinstance(end, bool) or not isinstance(end, int) or end < start:
            raise ValueError("invalid page range")
        by_label[item["label"]] = {**item, **normalize_source_metadata(item)}
    return by_label


def citation_labels(answer):
    """Reject mixed malformed/parenthesized citations, not just count good ones."""
    matches = list(LABEL.finditer(answer))
    if any((match.start() > 0 and answer[match.start() - 1] in "[［【")
           or (match.end() < len(answer) and answer[match.end()] in "]］】") for match in matches):
        raise ValueError("nested citation label")
    if NONCANONICAL_LABEL.search(LABEL.sub("", answer)):
        raise ValueError("noncanonical citation label")
    return {match[0][1:-1] for match in matches}


def source_metadata_suffix(item):
    metadata = normalize_source_metadata(item)
    if not any(value is not None for value in metadata.values()):
        return ""  # keep legacy source lists valid only when metadata is unknown
    return ("；发表日期（原文，未规范化）：" + json.dumps(metadata["publication_date"] or "检索结果未提供", ensure_ascii=False)
            + "；发表日期来源：" + json.dumps(metadata["publication_date_source"] or "检索结果未提供", ensure_ascii=False)
            + "；来源数据库：" + json.dumps(metadata["source_database"] or "检索结果未提供", ensure_ascii=False)
            + "；来源ID：" + json.dumps(metadata["source_id"] or "检索结果未提供", ensure_ascii=False))


def main(answer, citation_manifest):
    if not isinstance(answer, str) or not answer.strip() or len(answer) > 50000:
        return {"citation_ok": False, "verified_answer": REFUSAL, "cited_count": 0}
    try:
        by_label = _valid_manifest(citation_manifest)
        used = citation_labels(answer)
    except (TypeError, ValueError):
        return {"citation_ok": False, "verified_answer": REFUSAL, "cited_count": 0}
    if not used or not used.issubset(by_label):
        return {"citation_ok": False, "verified_answer": REFUSAL, "cited_count": 0}
    source_lines = []
    for label in sorted(used, key=lambda item: int(item[1:])):
        item = by_label[label]
        start, end = item.get("page_start"), item.get("page_end")
        page = "检索结果未提供" if start is None else (str(start) if start == end else str(start) + "–" + str(end))
        location = "；PDF页索引：" + page
        if item.get("resource_type") == "docx_ooxml":
            location = ("；同篇补充材料；母文档ID：" + item["parent_document_id"]
                        + "；DOCX XML定位：" + ", ".join(item["docx_xml_paths"])
                        + "；DOCX SHA-256：" + item["source_docx_sha256"])
        source_lines.append("[" + label + "] " + item["title"] + "；章节：" +
                            (item.get("section") or "检索结果未提供") + location +
                            "；DOI：" + (item.get("doi") or "检索结果未提供") +
                            source_metadata_suffix(item) +
                            "；Chunk ID：" + item["chunk_id"])
    heading = "来源（由检索记录生成，PDF 页索引从 0 开始）："
    if any(by_label[label].get("resource_type") == "docx_ooxml" for label in used):
        heading = "来源（由检索记录生成；PDF页索引从0开始，DOCX使用原始XML定位）："
    verified = answer.strip() + "\n\n" + heading + "\n" + "\n".join(source_lines)
    return {"citation_ok": True, "verified_answer": verified, "cited_count": len(used)}


_internal_citation_check = main


def public_main(answer, citation_manifest, source_registry=None):
    """Keep the internal audit intact; render only verified public sources."""
    checked = _internal_citation_check(answer, citation_manifest)
    if not checked["citation_ok"]:
        return checked
    by_label = _valid_manifest(citation_manifest)
    used = citation_labels(answer)
    # The model receives trace fields for audit purposes, but may not echo them.
    if re.search(r"chunk[ _-]?id|document[ _-]?id|文档ID|检索分数|docx_xml|SHA-?256|/w:document", answer, re.I):
        return {"citation_ok": False, "verified_answer": REFUSAL, "cited_count": 0}
    for item in by_label.values():
        identifiers = [item.get(k) for k in ("chunk_id", "document_id", "parent_document_id", "source_pdf_sha256", "source_docx_sha256")]
        if any(isinstance(v, str) and len(v) >= 12 and v in answer for v in identifiers):
            return {"citation_ok": False, "verified_answer": REFUSAL, "cited_count": 0}
    selected = []
    for label in sorted(used, key=lambda v: int(v[1:])):
        item = by_label[label]
        overlay = (source_registry or {}).get(item['document_id'])
        if overlay:
            if (overlay.get('source_pdf_sha256') != item.get('source_pdf_sha256')
                    or (item.get('doi') and item['doi'] != overlay.get('doi'))):
                return {"citation_ok": False, "verified_answer": REFUSAL, "cited_count": 0}
            item = {**item, 'title': overlay['title'], 'doi': overlay['doi']}
        doi = (item.get("doi") or "").strip()
        doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", doi, flags=re.I)
        selected.append((label, item, doi))
    main_roots = {}
    for label, item, doi in selected:
        if item.get("resource_type") == "docx_ooxml" or item.get("source_role") == "supporting_information":
            continue
        version = (item.get("material_version") or "", item.get("correction_of") or "")
        for alias in (item["document_id"], item["source_pdf_sha256"][:20]):
            main_roots.setdefault((doi.casefold(), version, alias), set()).add(item["source_pdf_sha256"])
    groups = {}
    for label, item, doi in selected:
        version = (item.get("material_version") or "", item.get("correction_of") or "")
        supporting = item.get("resource_type") == "docx_ooxml" or item.get("source_role") == "supporting_information"
        if supporting:
            parent = item.get("parent_document_id")
            roots = main_roots.get((doi.casefold(), version, parent), set())
            identity = ("pdf", next(iter(roots))) if len(roots) == 1 else (
                ("parent", parent) if parent and not roots else ("attachment", item["document_id"]))
        else:
            identity = ("pdf", item["source_pdf_sha256"])
        groups.setdefault((doi.casefold(), identity, version), []).append((label, item, supporting))
    if len(groups) == 1:
        answer = re.sub(r'(?m)^([ \t#*]*)跨论文归纳(?=[*：:\s]|$)', r'\1单篇论文内归纳', answer)
    lines = []
    for (doi, identity, version), members in groups.items():
        item = next((item for label, item, supporting in members if not supporting), members[0][1])
        if re.fullmatch(r'10\.[0-9]{4,9}/[^\s<>\[\]{}"\\]+', doi):
            # Encode URL delimiters without a sandbox dependency on urllib.
            safe = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~/"
            encoded = "".join(chr(b) if chr(b) in safe else "%%%02X" % b for b in doi.encode("utf-8"))
            source = "[DOI](https://doi.org/" + encoded + ")"
        elif item.get("source_database", "") == "PubMed" and re.fullmatch(r"[0-9]{1,16}", item.get("source_id") or ""):
            source = "[PubMed](https://pubmed.ncbi.nlm.nih.gov/" + item["source_id"] + "/)"
        else:
            source = "DOI及稳定官方链接未由检索元数据提供"
        if all(supporting for label, item, supporting in members):
            source = "母文" + source
        title = item["title"].replace("\n", " ").replace("\r", " ")
        labels = "、".join(label for label, item, supporting in members)
        heading = "**[" + labels + "] " + title + "**；" + source
        if version[0]:
            heading += "；版本：" + version[0]
        if version[1]:
            heading += "；勘误关联记录（独立列示）"
        lines.append(heading)
        summaries = {}
        section_names = {"OTHER": "", "METHODS": "方法", "RESULTS": "结果", "INTRODUCTION": "引言",
                         "DISCUSSION": "讨论", "CONCLUSION": "结论", "ABSTRACT": "摘要"}
        for label, item, supporting in members:
            location = item.get("supplement_locator") if supporting else None
            location = location or item.get("subsection") or item.get("section") or ""
            location = section_names.get(location, location)
            role = ("同篇补充材料（Supporting Information DOCX）" if item.get("resource_type") == "docx_ooxml"
                    else "同篇补充材料" if supporting else "正文")
            attachment = item.get("attachment_name") if supporting else None
            if attachment:
                role += "：" + re.split(r"[\\/]", attachment)[-1]
            lines.append("- [" + label + "] " + role + ("；" + location if location else "；章节定位未提供"))
            summary = item.get("material_summary") or "处理记录未提供，材料完整性未知"
            if not isinstance(summary, str) or len(summary) > 4000:
                return {"citation_ok": False, "verified_answer": REFUSAL, "cited_count": 0}
            summaries.setdefault(summary, []).append(label)
        if len(summaries) == 1:
            lines.append("材料状态：" + next(iter(summaries)))
        else:
            lines.append("材料状态：" + "；".join("/".join("[" + label + "]" for label in labels) + " " + summary
                         for summary, labels in summaries.items()))
        lines.append("")
    notices = list(dict.fromkeys(item[key] for label, item in by_label.items()
        for key in ("retrieval_notice", "material_notice")
        if (key == "retrieval_notice" or label in used) and isinstance(item.get(key), str) and item[key]))
    if any(len(value) > 4000 for value in notices):
        return {"citation_ok": False, "verified_answer": REFUSAL, "cited_count": 0}
    suffix = ("\n\n材料与范围限制：" + " ".join(notices)) if notices else ""
    return {"citation_ok": True, "verified_answer": answer.strip() + suffix + "\n\n参考来源：\n" + "\n".join(lines), "cited_count": len(used)}
