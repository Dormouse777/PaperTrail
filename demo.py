"""Offline fixture verification; does not execute MinerU, retrieval or a model."""
import hashlib
import json
from pathlib import Path


def run_demo():
    from pypdf import PdfReader
    from structure_aware_chunker import chunk_document
    from evidence_gate import main as gate
    from citation_audit import main as audit

    fixtures = Path(__file__).resolve().parent / "fixtures"
    document = json.loads((fixtures / "canonical.json").read_text("utf-8"))
    pdf = fixtures / "synthetic.pdf"
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    if digest != document["provenance"]["sourcePdfSha256"]:
        raise ValueError("DEMO_FIXTURE_HASH_MISMATCH")
    reader = PdfReader(pdf)
    if len(reader.pages) != 1:
        raise ValueError("DEMO_FIXTURE_PAGE_MISMATCH")
    extracted = reader.pages[0].extract_text()
    if any(block["text"] not in extracted for block in document["blocks"]):
        raise ValueError("DEMO_FIXTURE_TEXT_MISMATCH")
    chunks = chunk_document(document, target_tokens=80, max_tokens=120)
    if {chunk["section"] for chunk in chunks} != {"METHODS", "RESULTS"}:
        raise ValueError("DEMO_SECTION_COVERAGE_FAILED")
    # Explicit contract fixture, NOT an embedding/similarity search. Scores are
    # fixed fixture values; they cannot be used to measure retrieval quality.
    query = "What does the synthetic example say?"
    results = [{"chunk_id": c["chunk_id"], "document_id": c["document_id"],
        "title": c["title"], "text": c["raw_text"], "section": c["section"],
        "page_start": c["page_start"], "page_end": c["page_end"], "score": 1.0,
        "source_pdf_sha256": digest, "mineru_block_ids": c["mineru_block_ids"]} for c in chunks]
    evidence = gate(query, 200, json.dumps({"query": query, "dataset_id": "synthetic-contract-fixture", "results": results}))
    checked = audit("This synthetic example preserves section boundaries. [E1]", evidence["citation_manifest"])
    invalid_citation = audit("Unsupported citation. [E99]", evidence["citation_manifest"])
    empty = gate(query, 200, json.dumps({"query": query, "dataset_id": "synthetic-contract-fixture", "results": []}))
    if not evidence["has_evidence"] or not checked["citation_ok"] or invalid_citation["citation_ok"] or empty["has_evidence"]:
        raise ValueError("DEMO_EVIDENCE_CHECK_FAILED")
    return {"mode": "offline_synthetic_fixture", "scientific_evidence": False,
        "input": "self_authored_one_page_pdf_and_hand_authored_canonical_fixture",
        "source_pdf_sha256": digest, "pdf_pages_read": len(reader.pages),
        "chunks": [{"chunk_id": c["chunk_id"], "section": c["section"],
                    "page_start": c["page_start"], "block_ids": c["mineru_block_ids"],
                    "text": c["raw_text"]} for c in chunks],
        "token_count_method": "heuristic_estimate_not_model_tokenizer",
        "checks": {"fixture_pdf_text_readback": True, "section_boundaries": True,
                   "evidence_contract_accepted": evidence["has_evidence"],
                   "citation_membership_accepted": checked["citation_ok"],
                   "unknown_citation_rejected": not invalid_citation["citation_ok"],
                   "empty_evidence_refused": not empty["has_evidence"]},
        "not_executed": ["paper_download", "zotero", "mineru", "ragflow", "embedding", "rerank", "generation", "dify_service"],
        "network_requests": 0, "model_calls": 0, "writes": False}
