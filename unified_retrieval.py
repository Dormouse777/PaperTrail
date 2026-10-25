"""Federate confirmed import mappings without copying or embedding sources."""

import copy
import hashlib
import json
from pathlib import Path

from ragflow_prechunk_import import ImportError_
from ragflow_retrieve import load_mapping, normalize_retrieval, retrieval_request, validate_subqueries
from workflow_configuration import read_configuration


def load_sources(config_path):
    try:
        config = read_configuration(config_path)
    except ValueError:
        raise ImportError_("UNIFIED_SOURCES_INVALID") from None
    sources = config.get("sources")
    if not isinstance(sources, list) or not 1 <= len(sources) <= 8:
        raise ImportError_("UNIFIED_SOURCES_INVALID")
    names = set()
    for source in sources:
        if (not isinstance(source, dict) or not all(isinstance(source.get(key), str)
                and source[key].strip() for key in ("name", "dataset_id", "base_url", "state_dir"))
                or source["name"] in names or source.get("confirmed") is not True
                or not Path(source["state_dir"]).is_absolute()):
            raise ImportError_("UNIFIED_SOURCE_UNCONFIRMED")
        names.add(source["name"])
    return config


def evidence_identity(result):
    source_hash = result.get("source_docx_sha256") or result.get("source_pdf_sha256")
    if not source_hash:
        raise ImportError_("UNIFIED_SOURCE_IDENTITY_MISSING")
    role = result.get("source_role") or "main_text"
    if role in ("main_pdf", "main_text", "main"):
        role = "main_text"
    identity = [source_hash, role,
                result.get("material_version"), result.get("correction_of"),
                " ".join(result["text"].split())]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()


def _trace(result, source):
    return {"collection": source["name"], "dataset_id": source["dataset_id"],
            "document_id": result["document_id"], "chunk_id": result["chunk_id"],
            "ragflow_document_id": result["ragflow_document_id"],
            "ragflow_chunk_id": result["ragflow_chunk_id"]}


def _overlay(result, registry):
    result = copy.deepcopy(result)
    overlay = registry.get(result["document_id"])
    if overlay:
        if (overlay.get("source_pdf_sha256") != result.get("source_pdf_sha256")
                or result.get("doi") and result["doi"].casefold() != overlay["doi"].casefold()):
            raise ImportError_("UNIFIED_BIBLIOGRAPHY_SOURCE_CHANGED")
        result.update({key: overlay[key] for key in ("doi", "title")})
    return result


def _selection(values, name):
    if values is None:
        return None
    if (not isinstance(values, list) or not 1 <= len(values) <= 20
            or any(not isinstance(value, str) or not value.strip() or len(value) > 256 for value in values)
            or len(set(values)) != len(values)):
        raise ImportError_("UNIFIED_" + name + "_INVALID")
    return set(values)


def run_unified(api, config_path, question, top_n=5, similarity_threshold=.2,
                vector_similarity_weight=.3, subqueries=None, rrf_k=60,
                paper_ids=None, collections=None, material_scope=None):
    config = load_sources(config_path)
    papers = _selection(paper_ids, "PAPERS")
    selected_collections = _selection(collections, "COLLECTIONS")
    aliases = {"正文库": "main", "补充材料库": "supplement", "全部材料": "all"}
    if selected_collections and selected_collections.intersection(aliases):
        if len(selected_collections) != 1:
            raise ImportError_("UNIFIED_MATERIAL_COLLECTION_CONFLICT")
        selected_scope = aliases[next(iter(selected_collections))]
        if material_scope is not None and material_scope != selected_scope:
            raise ImportError_("UNIFIED_MATERIAL_SCOPE_CONFLICT")
        material_scope, selected_collections = selected_scope, None
    material_scope = material_scope or config.get("default_material_scope", "main")
    if material_scope not in ("main", "supplement", "all"):
        raise ImportError_("UNIFIED_MATERIAL_SCOPE_INVALID")
    sources = config["sources"]
    if selected_collections is not None:
        if selected_collections - {source["name"] for source in sources}:
            raise ImportError_("UNIFIED_COLLECTION_UNKNOWN")
        sources = [source for source in sources if source["name"] in selected_collections]
    queries = validate_subqueries(subqueries) if subqueries is not None else [question]
    if isinstance(rrf_k, bool) or not isinstance(rrf_k, int) or not 1 <= rrf_k <= 1000:
        raise ImportError_("UNIFIED_RRF_INVALID")
    retrieval_request(question, "unified", ["validation"], top_n,
                      similarity_threshold, vector_similarity_weight)
    available, unavailable, scoped = [], [], []
    known_papers = set()
    for source in sources:
        try:
            if source["base_url"].rstrip("/") != api.base:
                raise ImportError_("UNIFIED_ENDPOINT_MISMATCH")
            documents, mapping = load_mapping(Path(source["state_dir"]), api.base,
                                               source["dataset_id"], skip_incomplete=True,
                                               allow_empty=source.get("allow_empty") is True)
            projected = {}
            document_roles = {}
            for entry in mapping.values():
                role = entry["record"]["provenance"].get("source_role") or "main_text"
                category = "main" if role in ("main", "main_text", "main_pdf") else "supplement"
                document_roles.setdefault(entry["ragflow_document_id"], set()).add(category)
            if any(len(roles) != 1 for roles in document_roles.values()):
                raise ImportError_("UNIFIED_MIXED_DOCUMENT_ROLES")
            for remote_id, entry in mapping.items():
                record = entry["record"]
                projected_result = _overlay({"document_id": record["document_id"],
                    "title": record["title"], **record["provenance"]}, config.get("source_registry", {}))
                identities = {record["document_id"], record["provenance"].get("parent_document_id"),
                              projected_result.get("doi")}
                identities.discard(None)
                identities = {identity.casefold() for identity in identities}
                known_papers.update(identities)
                if material_scope != "all" and material_scope not in document_roles[entry["ragflow_document_id"]]:
                    continue
                if papers is None or identities.intersection(value.casefold() for value in papers):
                    projected[remote_id] = entry
            selected_documents = {entry["ragflow_document_id"] for entry in projected.values()}
            scoped.append((source, [value for value in documents if value in selected_documents], projected))
        except (ImportError_, OSError, ValueError):
            unavailable.append({"collection": source["name"], "reason": "mapping_unavailable"})
    if papers is not None and {value.casefold() for value in papers} - known_papers and not unavailable:
        raise ImportError_("UNIFIED_PAPER_UNKNOWN")
    fused = {}
    excluded_formulas = set()
    for source, documents, mapping in scoped:
        if not documents:
            available.append({"collection": source["name"], "documents": 0})
            continue
        source_results = []
        try:
            for query in queries:
                request = retrieval_request(query, source["dataset_id"], documents, 50,
                                            similarity_threshold, vector_similarity_weight)
                normalized = normalize_retrieval(query, source["dataset_id"], api.retrieve(request), mapping,
                                                 exclude_unreviewed_formulas=True)
                excluded_formulas.update((source["dataset_id"], row["document_id"], row["chunk_id"])
                                         for row in normalized.get("excluded_unreviewed_formulas", []))
                source_results.append((query, normalized["results"]))
        except (ImportError_, OSError, ValueError):
            unavailable.append({"collection": source["name"], "reason": "retrieval_unavailable"})
            continue
        available.append({"collection": source["name"], "documents": len(documents)})
        for query, results in source_results:
            unique_rank = 0
            seen = set()
            for result in results:
                result = _overlay(result, config.get("source_registry", {}))
                identity = evidence_identity(result)
                if identity in seen:
                    continue
                seen.add(identity)
                unique_rank += 1
                trace = _trace(result, source)
                entry = fused.setdefault(identity, {"result": result, "ranks": {}, "query_scores": {}, "source_mappings": []})
                entry["query_scores"][query] = max(entry["query_scores"].get(query, 0), result["score"])
                entry["result"]["score"] = max(entry["result"]["score"], result["score"])
                if trace not in entry["source_mappings"]:
                    entry["source_mappings"].append(trace)
    if not available:
        raise ImportError_("UNIFIED_ALL_SOURCES_UNAVAILABLE")
    for query in queries:
        ranking = sorted((identity for identity in fused if query in fused[identity]["query_scores"]),
                         key=lambda identity: (-fused[identity]["query_scores"][query], identity))
        for rank, identity in enumerate(ranking, 1):
            fused[identity]["ranks"][query] = rank
    ordered = sorted(fused.items(), key=lambda item: (
        -sum(1 / (rrf_k + rank) for rank in item[1]["ranks"].values()),
        min(item[1]["ranks"].values()), item[0]))[:top_n]
    results = []
    for identity, entry in ordered:
        result = entry["result"]
        result["source_mappings"] = entry["source_mappings"]
        result["original_chunk_id"] = result["chunk_id"]
        result["chunk_id"] = identity[:24]
        result["dataset_id"] = entry["source_mappings"][0]["dataset_id"]
        results.append(result)
    coverage = {"complete": not unavailable, "searched": available, "unavailable": unavailable,
                "material_scope": material_scope}
    labels = "、".join(item["collection"] for item in available)
    notice = ("本次实际检索：" + labels + "；部分资料来源暂不可用，不能视为完整覆盖。"
              if unavailable else "")
    notice += {"main": "本次仅检索正文；补充材料可按需启用。", "supplement": "本次仅检索补充材料。",
               "all": "本次检索正文与补充材料。"}[material_scope]
    if excluded_formulas:
        coverage["excluded_unreviewed_formula_chunks"] = len(excluded_formulas)
        notice += "部分数学公式片段尚未核实，整段未送入回答；本次证据不覆盖这些公式及相关计算。"
    return {"query": question, "dataset_id": "unified", "results": results, "total": len(fused),
            "coverage": coverage, "retrieval_notice": notice}
