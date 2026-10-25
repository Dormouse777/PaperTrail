#!/usr/bin/env python3
"""Retrieve only managed pre-chunks and restore traceable project evidence.

This is a local CLI/mapping layer, not yet the stable HTTP service for Dify.
It fails closed if the import is incomplete or a returned chunk differs from
the verified import snapshot. Never put RAGFLOW_API_KEY on the command line.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path

from ragflow_prechunk_import import (
    DEFAULT_BASE,
    DEFAULT_STATE_DIR,
    IMPORTER_MARKER,
    ImportError_,
    RagflowApi,
    state_path_for,
    text_digest,
)


SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")


def _nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ImportError_(f"Missing {name} in import state or retrieval result")
    return value


def load_mapping(state_dir: Path, base_url: str, dataset_id: str, *, skip_incomplete=False, allow_empty=False) -> tuple[list[str], dict[str, dict]]:
    """Read completed snapshots for this exact RAGFlow endpoint and dataset."""
    if not state_dir.is_dir():
        raise ImportError_(f"No import state directory: {state_dir}")
    document_ids: list[str] = []
    mapping: dict[str, dict] = {}
    project_chunk_ids: set[str] = set()
    source_ids: set[str] = set()
    base = base_url.rstrip("/")
    for path in sorted(state_dir.glob("*.json")):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ImportError_(f"Cannot read import state {path}: {exc}") from exc
        if not isinstance(state, dict):
            raise ImportError_(f"Import state is not an object: {path}")
        if state.get("importer") != IMPORTER_MARKER or state.get("base_url") != base or state.get("dataset_id") != dataset_id:
            continue
        if skip_incomplete and state.get("import_in_progress") is True:
            continue
        source_id = _nonempty(state.get("source_document_id"), "source_document_id")
        if path != state_path_for(state_dir, base, dataset_id, source_id):
            raise ImportError_(f"Import state filename does not match its scope: {path}")
        if source_id in source_ids:
            raise ImportError_(f"Duplicate import state for {source_id}")
        source_ids.add(source_id)
        if state.get("import_in_progress") is not False or not isinstance(state.get("completed_source_sha256"), str) or not SHA256.fullmatch(state["completed_source_sha256"]):
            raise ImportError_(f"Import for {source_id} is not complete; finish/recover it before retrieval")
        document_id = _nonempty(state.get("ragflow_document_id"), "ragflow_document_id")
        if document_id in document_ids:
            raise ImportError_(f"Duplicate RAGFlow document ID {document_id}")
        entries = state.get("entries")
        if not isinstance(entries, dict) or not entries:
            raise ImportError_(f"No chunk mapping for {source_id}")
        document_ids.append(document_id)
        xml_rows = [entry.get("record") for entry in entries.values()
                    if isinstance(entry, dict) and isinstance(entry.get("record"), dict)
                    and entry["record"].get("provenance", {}).get("resource_type") == "jats_xml"]
        if xml_rows:
            try:
                from jats_import_contract import validate_jats_rows
                if len(xml_rows) != len(entries):
                    raise ValueError("mixed sources")
                validate_jats_rows(xml_rows)
            except Exception:
                raise ImportError_("XML mapping no longer matches its verified source") from None
        docx_rows = [entry.get("record") for entry in entries.values()
                     if isinstance(entry, dict) and isinstance(entry.get("record"), dict)
                     and entry["record"].get("provenance", {}).get("resource_type") == "docx_ooxml"]
        if docx_rows:
            try:
                from docx_methods_source import validate_docx_rows
                if len(docx_rows) != len(entries):
                    raise ValueError("mixed sources")
                validate_docx_rows(docx_rows)
            except (ValueError, KeyError, TypeError, OSError, RuntimeError):
                raise ImportError_("DOCX mapping no longer matches its reviewed source") from None
        for ragflow_id, entry in entries.items():
            _nonempty(ragflow_id, "RAGFlow chunk ID")
            if ragflow_id in mapping:
                raise ImportError_(f"Duplicate RAGFlow chunk ID {ragflow_id}")
            if not isinstance(entry, dict) or not isinstance(entry.get("record"), dict):
                raise ImportError_(f"Malformed mapping for {ragflow_id}")
            record = entry["record"]
            project_id = _nonempty(record.get("chunk_id"), "project chunk_id")
            if project_id in project_chunk_ids:
                raise ImportError_(f"Duplicate project chunk ID {project_id}")
            project_chunk_ids.add(project_id)
            if record.get("document_id") != source_id:
                raise ImportError_(f"Source document mismatch for {ragflow_id}")
            for field in ("title", "raw_text", "embedding_text"):
                _nonempty(record.get(field), field)
            if not isinstance(record.get("mineru_block_ids"), list) or not isinstance(record.get("locations"), list) or not isinstance(record.get("provenance"), dict):
                raise ImportError_(f"Missing traceability fields for {ragflow_id}")
            if entry.get("content_sha256") != text_digest(record["embedding_text"]):
                raise ImportError_(f"Snapshot content hash mismatch for {ragflow_id}")
            mapping[ragflow_id] = {"record": record, "ragflow_document_id": document_id}
    if not document_ids and not allow_empty:
        raise ImportError_(f"No completed imports for dataset {dataset_id}; online import is required first")
    return document_ids, mapping


def retrieval_request(question: str, dataset_id: str, document_ids: list[str], top_n: int,
                      similarity_threshold: float = 0.2, vector_similarity_weight: float = 0.3) -> dict:
    _nonempty(question, "question")
    _nonempty(dataset_id, "dataset_id")
    if not 1 <= top_n <= 50:
        raise ImportError_("top-n must be between 1 and 50")
    if not document_ids:
        raise ImportError_("No managed documents to search")
    for name, value in (("similarity-threshold", similarity_threshold), ("vector-weight", vector_similarity_weight)):
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ImportError_(f"{name} must be between 0 and 1")
    return {"question": question, "dataset_ids": [dataset_id], "document_ids": document_ids,
            "page": 1, "page_size": top_n, "rerank_candidates_count": max(64, top_n),
            "similarity_threshold": similarity_threshold, "vector_similarity_weight": vector_similarity_weight,
            "keyword": False, "highlight": False, "use_kg": False, "toc_enhance": False,
            "include_knowledge_compilation": False}


def normalize_retrieval(question: str, dataset_id: str, data: dict, mapping: dict[str, dict],
                        *, exclude_unreviewed_formulas: bool = False) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("chunks"), list):
        raise ImportError_("RAGFlow retrieval has no chunk list")
    results = []
    excluded_formulas = []
    seen: set[str] = set()
    for chunk in data["chunks"]:
        if not isinstance(chunk, dict):
            raise ImportError_("RAGFlow returned a non-object chunk")
        ragflow_id = _nonempty(chunk.get("id"), "RAGFlow chunk ID")
        if ragflow_id in seen:
            raise ImportError_(f"RAGFlow returned duplicate chunk {ragflow_id}")
        seen.add(ragflow_id)
        snapshot = mapping.get(ragflow_id)
        if snapshot is None:
            raise ImportError_(f"Unmapped retrieval chunk {ragflow_id}; refusing an untraceable citation")
        if chunk.get("dataset_id") != dataset_id or chunk.get("document_id") != snapshot["ragflow_document_id"]:
            raise ImportError_(f"Dataset/document mismatch for retrieval chunk {ragflow_id}")
        record = snapshot["record"]
        if not isinstance(chunk.get("content"), str) or text_digest(chunk["content"]) != text_digest(record["embedding_text"]):
            raise ImportError_(f"Indexed content differs from import snapshot for {ragflow_id}")
        # A source hash proves byte identity, not formula correctness. Saved
        # MinerU equations carry pending review flags; do not silently drop
        # those flags and hand known unreviewed mathematics to an answer model.
        # Legacy inline LaTeX without equation metadata remains outside this
        # gate's coverage and still needs separate source review.
        formula_marked = (record.get("content_type") == "equation"
            or (isinstance(record.get("content_types"), list)
                and "equation" in record["content_types"])
            or "formula_review_required" in record
            or "formula_semantics_verified" in record)
        if formula_marked and (record.get("formula_review_required") is not False
                or record.get("formula_semantics_verified") is not True):
            if exclude_unreviewed_formulas:
                # Identity and indexed bytes were checked above. Quarantine the
                # whole mixed chunk; never remove its equation or clear flags.
                excluded_formulas.append({"document_id": record["document_id"],
                                          "chunk_id": record["chunk_id"]})
                continue
            raise ImportError_("FORMULA_REVIEW_REQUIRED: unreviewed formula evidence cannot be delivered")
        score = chunk.get("similarity")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or score < 0:
            raise ImportError_(f"Invalid retrieval score for {ragflow_id}")
        provenance = record["provenance"]
        if (provenance.get("ocr_review_required") is True
                and provenance.get("ocr_verified") is not True):
            raise ImportError_("OCR_REVIEW_REQUIRED")
        restored = {
            "chunk_id": record["chunk_id"], "score": score,
            "document_id": record["document_id"], "title": record["title"],
            "section": record.get("section"), "subsection": record.get("subsection"),
            "page_start": record.get("page_start"), "page_end": record.get("page_end"),
            "text": record["raw_text"], "content_type": record.get("content_type"),
            "mineru_block_ids": record["mineru_block_ids"], "locations": record["locations"],
            "source_pdf_sha256": provenance.get("source_pdf_sha256"),
            "zotero_parent_key": provenance.get("zotero_parent_key"),
            "zotero_pdf_attachment_key": provenance.get("zotero_pdf_attachment_key"),
            "doi": provenance.get("doi"),
            "source_database": provenance.get("source_database"),
            "source_id": provenance.get("source_id"),
            "publication_date": provenance.get("publication_date"),
            "publication_date_source": provenance.get("publication_date_source"),
            "ragflow_chunk_id": ragflow_id,
            "ragflow_document_id": snapshot["ragflow_document_id"],
        }
        if formula_marked:
            restored["formula_review_required"] = False
            restored["formula_semantics_verified"] = True
        if provenance.get("resource_type") == "jats_xml":
            for field in ("resource_type", "source_xml_sha256", "source_parser", "source", "endpoint"):
                restored[field] = provenance.get(field)
            for field in ("xml_anchors", "jats_block_ids"):
                restored[field] = record.get(field)
            try:
                from jats_import_contract import validate_jats_result_trace
                validate_jats_result_trace(restored)
            except Exception:
                raise ImportError_("Mapped retrieval result lacks valid XML source trace") from None
        elif provenance.get("resource_type") == "docx_ooxml":
            if provenance.get("zotero_attachment_key"):
                restored["zotero_attachment_key"] = provenance["zotero_attachment_key"]
            for field in ("resource_type", "source_parser", "source_docx_sha256",
                          "source_document_xml_sha256", "source_url", "source_role",
                          "parent_document_id", "pmcid", "pmid", "license_code"):
                restored[field] = provenance.get(field)
            for field in ("docx_block_ids", "docx_xml_paths"):
                restored[field] = record.get(field)
            try:
                from docx_methods_source import validate_docx_result_trace
                validate_docx_result_trace(restored)
            except (ValueError, TypeError):
                raise ImportError_("Mapped retrieval result lacks valid DOCX source trace") from None
        elif provenance.get("resource_type") not in (None, "pdf"):
            raise ImportError_("Unsupported retrieval resource type")
        for field in ("source_role", "parent_document_id", "material_version", "correction_of",
                      "attachment_name", "zotero_attachment_key", "source_url"):
            if field in provenance:
                restored[field] = provenance[field]
        results.append(restored)
    total = data.get("total")
    if total is not None and (isinstance(total, bool) or not isinstance(total, int) or total < len(results)):
        raise ImportError_("Invalid retrieval total")
    result = {"query": question, "dataset_id": dataset_id, "results": results,
              "total": total if total is not None else len(results)}
    if excluded_formulas:
        result["excluded_unreviewed_formulas"] = excluded_formulas
    return result


def normalize_mcp_retrieval(question: str, dataset_id: str, data: dict,
                            mapping: dict[str, dict]) -> dict:
    """Restore trusted project provenance onto raw RAGFlow MCP chunks.

    MCP is the retrieval source. Only chunk IDs, exact indexed content,
    document/dataset identity, and similarity are accepted from its payload;
    all citation metadata is restored from the verified import snapshot.
    """
    if not isinstance(data, dict) or not isinstance(data.get("chunks"), list):
        raise ImportError_("MCP retrieval has no chunk list")
    if len(data["chunks"]) > 50:
        raise ImportError_("MCP retrieval exceeds the 50-chunk normalization limit")
    adapted_chunks = []
    for chunk in data["chunks"]:
        if not isinstance(chunk, dict):
            raise ImportError_("MCP returned a non-object chunk")
        explicit_dataset = chunk.get("dataset_id")
        ragflow_dataset = chunk.get("kb_id")
        if 'dataset_id' in chunk and 'kb_id' in chunk and explicit_dataset != ragflow_dataset:
            raise ImportError_("MCP chunk contains conflicting dataset identifiers")
        explicit_document = chunk.get("document_id")
        ragflow_document = chunk.get("doc_id")
        if 'document_id' in chunk and 'doc_id' in chunk and explicit_document != ragflow_document:
            raise ImportError_("MCP chunk contains conflicting document identifiers")
        adapted = dict(chunk)
        adapted["dataset_id"] = explicit_dataset if 'dataset_id' in chunk else ragflow_dataset
        adapted["document_id"] = explicit_document if 'document_id' in chunk else ragflow_document
        adapted_chunks.append(adapted)
    normalized = normalize_retrieval(question, dataset_id,
                                     {**data, "chunks": adapted_chunks}, mapping)
    for result in normalized["results"]:
        if result.get("resource_type") == "jats_xml":
            continue  # The separate XML source/element gate ran above.
        if not SHA256.fullmatch(str(result.get("source_pdf_sha256") or "")):
            raise ImportError_("Mapped MCP result has no valid source PDF SHA-256")
        if not result.get("mineru_block_ids") or not result.get("locations"):
            raise ImportError_("Mapped MCP result has no MinerU trace")
    return normalized


def validate_subqueries(subqueries: list[str]) -> list[str]:
    if not isinstance(subqueries, list) or not 1 <= len(subqueries) <= 8:
        raise ImportError_("subqueries must contain 1–8 questions")
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in subqueries:
        question = _nonempty(value, "subquery").strip()
        if len(question) > 4000:
            raise ImportError_("subquery must be at most 4000 characters")
        if question in seen:
            raise ImportError_("subqueries must be unique")
        seen.add(question)
        cleaned.append(question)
    return cleaned


def fuse_retrieval_results(query: str, dataset_id: str, responses: list[dict],
                           top_n: int, rrf_k: int = 60) -> dict:
    """Fuse normalized rankings with reciprocal-rank fusion.

    Each evidence record is emitted once. RRF determines ordering, while the
    public ``score`` remains the best upstream similarity for compatibility
    with the evidence-quality threshold. Raw scores never determine fusion
    order; each was already subject to the same upstream similarity gate.
    """
    _nonempty(query, "query")
    _nonempty(dataset_id, "dataset_id")
    if not 1 <= top_n <= 50:
        raise ImportError_("top-n must be between 1 and 50")
    if isinstance(rrf_k, bool) or not isinstance(rrf_k, int) or not 1 <= rrf_k <= 1000:
        raise ImportError_("rrf-k must be between 1 and 1000")
    if not isinstance(responses, list) or not 1 <= len(responses) <= 8:
        raise ImportError_("fusion needs 1–8 normalized responses")

    fused: dict[str, dict] = {}
    first_seen = 0
    source_queries: list[str] = []
    for response in responses:
        if not isinstance(response, dict) or response.get("dataset_id") != dataset_id:
            raise ImportError_("Cannot fuse retrieval results from another dataset")
        source_query = _nonempty(response.get("query"), "source query")
        if source_query in source_queries:
            raise ImportError_("Cannot fuse duplicate source queries")
        source_queries.append(source_query)
        results = response.get("results")
        if not isinstance(results, list):
            raise ImportError_("Normalized retrieval response has no results")
        for rank, result in enumerate(results, 1):
            if not isinstance(result, dict):
                raise ImportError_("Normalized retrieval result is not an object")
            chunk_id = _nonempty(result.get("chunk_id"), "chunk_id")
            if chunk_id not in fused:
                first_seen += 1
                fused[chunk_id] = {
                    "result": dict(result),
                    "rrf_score": 0.0,
                    "max_similarity": result.get("score"),
                    "best_rank": rank,
                    "first_seen": first_seen,
                    "matches": [],
                }
            entry = fused[chunk_id]
            entry["rrf_score"] += 1.0 / (rrf_k + rank)
            entry["max_similarity"] = max(entry["max_similarity"], result.get("score"))
            entry["best_rank"] = min(entry["best_rank"], rank)
            entry["matches"].append({"query": source_query, "rank": rank,
                                     "score": result.get("score")})

    ordered = sorted(fused.values(), key=lambda entry: (
        -entry["rrf_score"], entry["best_rank"], entry["first_seen"]
    ))[:top_n]
    results = []
    for entry in ordered:
        result = entry["result"]
        result["ragflow_score"] = entry["max_similarity"]
        result["score"] = entry["max_similarity"]
        result["fusion_score"] = entry["rrf_score"]
        result["retrieval_matches"] = entry["matches"]
        results.append(result)
    return {
        "query": query,
        "queries": source_queries,
        "dataset_id": dataset_id,
        "results": results,
        "total": len(fused),
        "fusion": {"method": "reciprocal_rank_fusion", "rrf_k": rrf_k},
    }


def run(api: RagflowApi, dataset_id: str, state_dir: Path, question: str, top_n: int,
        similarity_threshold: float = 0.2, vector_similarity_weight: float = 0.3) -> dict:
    document_ids, mapping = load_mapping(state_dir, api.base, dataset_id)
    request = retrieval_request(question, dataset_id, document_ids, top_n,
                                similarity_threshold, vector_similarity_weight)
    return normalize_retrieval(question, dataset_id, api.retrieve(request), mapping)


def run_multi(api: RagflowApi, dataset_id: str, state_dir: Path, query: str,
              subqueries: list[str], top_n: int, similarity_threshold: float = 0.2,
              vector_similarity_weight: float = 0.3, rrf_k: int = 60) -> dict:
    questions = validate_subqueries(subqueries)
    document_ids, mapping = load_mapping(state_dir, api.base, dataset_id)
    responses = []
    for question in questions:
        request = retrieval_request(question, dataset_id, document_ids, top_n,
                                    similarity_threshold, vector_similarity_weight)
        responses.append(normalize_retrieval(
            question, dataset_id, api.retrieve(request), mapping
        ))
    return fuse_retrieval_results(query, dataset_id, responses, top_n, rrf_k)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--question", required=True)
    parser.add_argument("--subquery", action="append",
                        help="Retrieve multiple evidence aspects and fuse them (repeatable, max 8)")
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--transport-base-url", help="Explicit HTTP API root; mapping identity stays --base-url")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--similarity-threshold", type=float, default=0.2)
    parser.add_argument("--vector-weight", type=float, default=0.3)
    parser.add_argument("--rrf-k", type=int, default=60)
    args = parser.parse_args(argv)
    try:
        key = os.environ.get("RAGFLOW_API_KEY", "")
        if not key:
            raise ImportError_("RAGFLOW_API_KEY is required in the environment")
        api = RagflowApi(args.base_url, key, transport_base_url=args.transport_base_url)
        if args.subquery:
            result = run_multi(api, args.dataset_id, args.state_dir, args.question,
                               args.subquery, args.top_n, args.similarity_threshold,
                               args.vector_weight, args.rrf_k)
        else:
            result = run(api, args.dataset_id, args.state_dir, args.question, args.top_n,
                         args.similarity_threshold, args.vector_weight)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ImportError_, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
