"""Finite, explicitly authorized online audits for the Zotero scope workflow.

Uses existing Zotero/RAGFlow clients. This module does not acquire materials,
mutate Zotero/index data, start services, or call an answer generation model.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit
import urllib.request

from zotero_scope_workflow import (live_identity, local_audit, local_mappings,
    read, scope_digest, sha, validate_authorization, validate_scope)
from zotero_local_api import ZoteroLocalClient
from process_lock import process_lock
from ragflow_prechunk_import import (IMPORTER_MARKER, ImportError_, RagflowApi,
    load_bundle, paper_metadata, state_path_for)


class AuditStop(RuntimeError):
    """A common-service, identity, quota or uncertain-dispatch stop."""


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def digest_value(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode()).hexdigest()


class AuditBudget:
    """Consume approved quotas before dispatch; never refund uncertain calls."""
    def __init__(self, scope, authorization, path):
        validate_authorization(scope, authorization)
        self.scope, self.authorization, self.path = copy.deepcopy(scope), copy.deepcopy(authorization), Path(path)
        self.binding = {"scope_sha256": scope_digest(scope),
                        "authorization_sha256": digest_value(authorization)}
        if type(authorization.get("limits")) is not dict:
            raise AuditStop("BATCH_LIMITS_REQUIRED")
        with process_lock(self.path):
            if self.path.exists():
                self.snapshot()
            else:
                atomic_json(self.path, {**self.binding, "counts": {}, "events": [],
                                       "reserved_cost_upper_cny": "0"})

    def snapshot(self):
        state = read(self.path)
        if any(state.get(k) != v for k, v in self.binding.items()):
            raise AuditStop("BUDGET_BELONGS_TO_ANOTHER_AUTHORIZATION")
        return state

    def reserve(self, operation, amounts, *, cost=Decimal(0), replay_safe=True):
        validate_authorization(self.scope, self.authorization)
        closure_path = self.path.parent / "batch-closure.json"
        if closure_path.is_file():
            closure = read(closure_path)
            if closure.get("closed") is True and all(closure.get(k) == v for k, v in self.binding.items()):
                raise AuditStop("CURRENT_BATCH_CLOSED")
        with process_lock(self.path):
            state = self.snapshot()
            if not replay_safe and any(e["operation"] == operation for e in state["events"]):
                raise AuditStop("DISPATCH_ALREADY_RECORDED_RECONCILE_FIRST")
            counts = dict(state["counts"])
            for name, amount in amounts.items():
                limit = self.authorization["limits"].get(name)
                if type(amount) is not int or amount < 0 or type(limit) is not int or limit < 0:
                    raise AuditStop("INVALID_OR_MISSING_LIMIT:" + name)
                counts[name] = counts.get(name, 0) + amount
                if counts[name] > limit:
                    raise AuditStop("BATCH_LIMIT_EXHAUSTED:" + name)
            cap = Decimal(str(self.authorization["limits"].get("total_cost_cny", "0")))
            spent = Decimal(state["reserved_cost_upper_cny"]) + cost
            if not cost.is_finite() or cost < 0 or not cap.is_finite() or spent > cap:
                raise AuditStop("BATCH_COST_LIMIT_EXHAUSTED")
            event_id = len(state["events"])
            state["counts"] = counts
            state["reserved_cost_upper_cny"] = str(spent)
            state["events"].append({"id": event_id, "operation": operation, "amounts": amounts,
                "reserved_cost_upper_cny": str(cost), "status": "pending",
                "at": datetime.now(timezone.utc).isoformat()})
            atomic_json(self.path, state)
            return event_id

    def finish(self, event_id, *, response=None, failed=False):
        with process_lock(self.path):
            state = self.snapshot()
            event = state["events"][event_id]
            if event["status"] != "pending":
                raise AuditStop("DISPATCH_ACK_ALREADY_RECORDED")
            event["status"] = "failed" if failed else "acknowledged"
            if response is not None:
                event["response_sha256"] = digest_value(response)
            atomic_json(self.path, state)


def bounded_zotero(scope, budget):
    parents = {p["key"] for p in scope["papers"]}
    collection = scope.get("collection_key")
    class ReadClient(ZoteroLocalClient):
        def _send_once(self, url, method, body, headers, *, timeout=None):
            uri = urlsplit(url)
            path = uri.path.removeprefix("/api")
            match = re.fullmatch(r"/users/0/items/([A-Z0-9]{8})(/children)?", path)
            allowed = (path in ("", "/") or match and match[1] in parents or collection and
                path in (f"/users/0/collections/{collection}", f"/users/0/collections/{collection}/items/top"))
            if (method != "GET" or body is not None or uri.scheme != "http"
                    or uri.netloc != "127.0.0.1:23119" or not uri.path.startswith("/api") or not allowed):
                raise AuditStop("ZOTERO_AUDIT_REQUEST_OUT_OF_SCOPE")
            event = budget.reserve("zotero:" + path, {"zotero_http_requests": 1})
            try:
                result = super()._send_once(url, method, body, headers, timeout=timeout)
            except (OSError, RuntimeError):
                budget.finish(event, failed=True)
                raise
            budget.finish(event, response={"status": result[0], "bytes_sha256": hashlib.sha256(result[1]).hexdigest()})
            return result
    return ReadClient(timeout=15)


def bounded_index_api(config, mappings, budget, *, key, transport):
    """Allow only the exact known document lookups and complete chunk listings."""
    allowed_transport = {"http://127.0.0.1:80/api/v1", "http://127.0.0.1:9380/api/v1"}
    if transport not in allowed_transport or not key:
        raise AuditStop("LOCAL_INDEX_TRANSPORT_AND_KEY_REQUIRED")
    names, chunks = set(), set()
    bases = set()
    for mapping in mappings:
        source = mapping["source"]
        bases.add(source["base_url"].rstrip("/"))
        names.add((source["dataset_id"], "trae-prechunk-" + mapping["document_id"] + ".md"))
        for entry in mapping["entries"].values():
            chunks.add((source["dataset_id"], entry["ragflow_document_id"]))
    if len(bases) != 1:
        raise AuditStop("INDEX_CANONICAL_BASE_AMBIGUOUS")
    class ReadApi(RagflowApi):
        def _json(self, method, path, body=None):
            uri = urlsplit(path)
            match = re.fullmatch(r"/datasets/([^/]+)/documents(?:/([^/]+)/chunks)?", uri.path)
            query = parse_qs(uri.query)
            allowed = bool(match and ((match[2] and (match[1], match[2]) in chunks
                and set(query) == {"page", "page_size"}) or (not match[2]
                and (match[1], query.get("keywords", [None])[0]) in names
                and set(query) == {"keywords", "page", "page_size"})))
            if method != "GET" or body is not None or not allowed:
                raise AuditStop("INDEX_AUDIT_REQUEST_OUT_OF_SCOPE")
            event = budget.reserve("index:" + path, {"index_and_readback_http_requests": 1})
            try:
                response = super()._json(method, path, body)
            except (OSError, RuntimeError):
                budget.finish(event, failed=True)
                raise
            budget.finish(event, response=response)
            return response
    return ReadApi(next(iter(bases)), key, transport_base_url=transport)


def verify_remote_mapping(api, mapping, bundle):
    """Exact content/IDs/counters and necessary metadata, with no repair writes."""
    source, entries = mapping["source"], mapping["entries"]
    if api.base != source["base_url"].rstrip("/") or bundle["document_id"] != mapping["document_id"]:
        raise AuditStop("READBACK_SOURCE_IDENTITY_DIFFERS")
    expected_records = {e["record"]["chunk_id"]: e["record"] for e in entries.values()}
    if {r["chunk_id"]: r for r in bundle["rows"]} != expected_records:
        return {"status": "differs", "reason": "LOCAL_CANDIDATE_MAPPING_DIFFERS"}
    ids = {e["ragflow_document_id"] for e in entries.values()}
    if len(ids) != 1:
        raise AuditStop("MAPPING_DOCUMENT_ID_AMBIGUOUS")
    remote_id = next(iter(ids))
    document = api.find_document(source["dataset_id"], bundle["document_name"])
    if document is None:
        return {"status": "missing", "reason": "REMOTE_DOCUMENT_MISSING"}
    metadata = document.get("meta_fields") or {}
    if (document.get("id") != remote_id or document.get("type") != "virtual"
            or metadata.get("codex_importer") != IMPORTER_MARKER
            or metadata.get("project_document_id") != bundle["document_id"]):
        raise AuditStop("REMOTE_DOCUMENT_OWNERSHIP_OR_IDENTITY_DIFFERS")
    if any(metadata.get(k) != v for k, v in paper_metadata(bundle).items()):
        return {"status": "differs", "reason": "REMOTE_METADATA_DIFFERS"}
    listing = api.list_chunks(source["dataset_id"], remote_id)
    remote = listing.get("chunks", [])
    actual = {r["id"]: r["content"] for r in remote}
    expected = {rid: e["record"]["embedding_text"] for rid, e in entries.items()}
    if (actual != expected or len(actual) != len(remote) or listing.get("total") != len(expected)
            or document.get("chunk_count") != len(expected)
            or (listing.get("doc") or {}).get("chunk_count") != len(expected)):
        return {"status": "differs", "reason": "REMOTE_BODY_OR_COUNTER_DIFFERS"}
    return {"status": "verified", "document_id": remote_id, "chunks": len(expected),
            "metadata_sha256": digest_value(metadata), "contents_sha256": digest_value(actual)}


def validate_formal_hits(payload, mapping, doi):
    if not isinstance(payload, dict) or payload.get("dataset_id") != "unified" or not isinstance(payload.get("results"), list):
        raise AuditStop("FORMAL_DEFAULT_RESPONSE_INVALID")
    coverage = payload.get("coverage", {})
    if not isinstance(coverage, dict) or coverage.get("complete") is not True or coverage.get("unavailable"):
        raise AuditStop("FORMAL_DEFAULT_COVERAGE_INCOMPLETE")
    matches = []
    for hit in payload["results"]:
        source_mappings = hit.get("source_mappings", [])
        for origin in source_mappings:
            if origin.get("dataset_id") != mapping["source"]["dataset_id"]:
                continue
            entry = mapping["entries"].get(origin.get("ragflow_chunk_id"))
            if not entry:
                continue
            record = entry["record"]
            source_hash = record["provenance"].get("source_pdf_sha256") or record["provenance"].get("source_docx_sha256")
            role = record["provenance"].get("source_role", "main_pdf")
            comparisons = {"text": record["raw_text"], "page_start": record.get("page_start"),
                "page_end": record.get("page_end"), "mineru_block_ids": record["mineru_block_ids"],
                "locations": record["locations"]}
            if (hit.get("doi", "").lower() != doi.lower() or hit.get("document_id") != record["document_id"]
                    or (hit.get("source_pdf_sha256") or hit.get("source_docx_sha256")) != source_hash
                    or origin.get("ragflow_document_id") != entry["ragflow_document_id"]
                    or any(hit.get(k) != v for k, v in comparisons.items())
                    or hit.get("source_role", "main_pdf") != role):
                raise AuditStop("FORMAL_DEFAULT_EVIDENCE_DIFFERS")
            if role != "main_pdf" and not hit.get("attachment_name"):
                raise AuditStop("FORMAL_DEFAULT_ATTACHMENT_IDENTITY_MISSING")
            if not record["locations"] or not record["mineru_block_ids"]:
                raise AuditStop("FORMAL_DEFAULT_LOCATION_MISSING")
            matches.append(record["chunk_id"])
    return {"status": "verified" if matches else "not_recalled", "matched_project_chunk_ids": sorted(set(matches))}


def formal_health(config, mappings, budget, *, sender=None):
    """A fresh non-embedding health read is required even for cached recall."""
    event = budget.reserve("formal-health", {"index_and_readback_http_requests": 1})
    if sender is None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoBridgeRedirect())
        with opener.open("http://127.0.0.1:8766/healthz", timeout=15) as response:
            raw = response.read(65537)
            if len(raw) > 65536:
                raise AuditStop("FORMAL_HEALTH_RESPONSE_TOO_LARGE")
            value = json.loads(raw)
    else:
        value = sender()
    budget.finish(event, response=value)
    sources = [s for s in config["sources"] if s.get("confirmed") is True]
    expected = {(s["name"], sum(m["source"]["dataset_id"] == s["dataset_id"] for m in mappings),
                 sum(len(m["entries"]) for m in mappings if m["source"]["dataset_id"] == s["dataset_id"])) for s in sources}
    actual = {(s.get("collection"), s.get("documents"), s.get("chunks")) for s in value.get("sources", [])}
    if (value.get("status") != "ready" or value.get("scope") != "unified"
            or value.get("source_count") != len(sources) or value.get("coverage_complete") is not True
            or value.get("unavailable") or actual != expected):
        raise AuditStop("FORMAL_DEFAULT_HEALTH_OR_SCOPE_DIFFERS")
    return {"status": "verified", "sources": value["sources"], "cached_recall_requires_this_fresh_health": True}


class _NoBridgeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _query_cost(query, source_count, authorization):
    proof = authorization.get("embedding_price_proof", {})
    try:
        price = Decimal(str(proof["cny_per_million_input_tokens"]))
        checked = datetime.fromisoformat(proof["checked_at"])
        valid = (price.is_finite() and price > 0 and checked.tzinfo is not None
            and abs((datetime.now(timezone.utc) - checked).total_seconds()) <= 86400
            and proof["provider_endpoint"] == "https://api.siliconflow.cn/v1/embeddings"
            and proof["model"] and proof["official_source_url"].startswith("https://")
            and proof.get("runtime_configuration_verified") is True)
    except (KeyError, ValueError, InvalidOperation, TypeError):
        valid = False
    if not valid:
        raise AuditStop("CURRENT_EMBEDDING_PRICE_AND_RUNTIME_PROOF_REQUIRED")
    # Conservative byte bound includes provider special-token/template margin.
    tokens = (len(query.encode("utf-8")) + 128) * source_count
    return tokens, price * Decimal(tokens) / Decimal(1000000)


def formal_retrieval(query, mapping, doi, config, budget, directory, *, token, sender=None):
    if not token or not query.strip() or len(query) > 4000:
        raise AuditStop("FORMAL_QUERY_AND_TOKEN_REQUIRED")
    binding = {"query": query, "doi": doi, "mapping_sha256": digest_value(mapping),
               "configuration_sha256": digest_value(config), "endpoint": "http://127.0.0.1:8766/retrieve"}
    identifier = digest_value(binding)
    path = Path(directory) / (identifier + ".json")
    with process_lock(path):
        if path.exists():
            checkpoint = read(path)
            if checkpoint.get("binding") != binding:
                raise AuditStop("FORMAL_QUERY_CHECKPOINT_DIFFERS")
            if checkpoint.get("status") != "acknowledged":
                raise AuditStop("FORMAL_QUERY_DISPATCH_UNCERTAIN_NO_REPLAY")
            saved_event = checkpoint.get("budget_event")
            ledger = budget.snapshot()
            if type(saved_event) is not int or not 0 <= saved_event < len(ledger["events"]):
                raise AuditStop("FORMAL_QUERY_BUDGET_CHECKPOINT_MISSING")
            event = ledger["events"][saved_event]
            if event["operation"] != "formal:" + identifier:
                raise AuditStop("FORMAL_QUERY_BUDGET_CHECKPOINT_DIFFERS")
            if event["status"] == "pending":
                # The durable complete response proves acknowledgement after
                # a crash between response-save and ledger-save; no replay.
                budget.finish(saved_event, response=checkpoint["response"])
            elif (event["status"] != "acknowledged"
                    or event.get("response_sha256") != digest_value(checkpoint["response"])):
                raise AuditStop("FORMAL_QUERY_SAVED_RESPONSE_DIFFERS")
            result = validate_formal_hits(checkpoint["response"], mapping, doi)
            return {**result, "reused_query_checkpoint": True, "checkpoint": str(path)}
        source_count = len([s for s in config["sources"] if s.get("confirmed") is True])
        proof_path = budget.path.parent / "embedding-price-proof.json"
        pricing = dict(budget.authorization)
        if proof_path.is_file():
            pricing["embedding_price_proof"] = read(proof_path)
        tokens, cost = _query_cost(query, source_count, pricing)
        event = budget.reserve("formal:" + identifier, {"formal_retrieval_queries": 1,
            "maximum_query_embedding_dispatches": source_count, "embedding_input_tokens": tokens},
            cost=cost, replay_safe=False)
        checkpoint = {"binding": binding, "status": "pending", "budget_event": event}
        atomic_json(path, checkpoint)
        payload = {"query": query, "top_n": 10}
        if sender is None:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoBridgeRedirect())
            request = urllib.request.Request(binding["endpoint"], data=json.dumps(payload).encode(),
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"}, method="POST")
            with opener.open(request, timeout=120) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise AuditStop("FORMAL_RESPONSE_TOO_LARGE")
                response_value = json.loads(raw)
        else:
            response_value = sender(payload)
        checkpoint.update(status="acknowledged", response=response_value)
        atomic_json(path, checkpoint)
        budget.finish(event, response=response_value)
        result = validate_formal_hits(response_value, mapping, doi)
        return {**result, "reused_query_checkpoint": False, "checkpoint": str(path)}


def run_live_audit(scope, config, *, authorization, output, prepared_root, paper_root,
                   transport="http://127.0.0.1:80/api/v1", verify_retrieval=False,
                   client=None, api=None, retrieval_sender=None, health_sender=None):
    """Same explicit scope as offline audit; record every paper even after stop."""
    validate_scope(scope, config)
    validate_authorization(scope, authorization)
    output = Path(output)
    with process_lock(output / "online-audit"):
        budget = AuditBudget(scope, authorization, output / "online-budget.json")
        initial = budget.snapshot()
        report = local_audit(scope, config, prepared_root=prepared_root, paper_root=paper_root)
        report.update(mode="live-audit", common_stop=None, retrieval_requested=verify_retrieval)
        try:
            from zotero_scope_workflow import known_supplement_roles
            snapshot = live_identity(scope, client if client is not None else bounded_zotero(scope, budget),
                                     attachment_roles=known_supplement_roles(scope, paper_root))
            atomic_json(output / "identity-snapshot.json", snapshot)
            report = {**local_audit(scope, config, prepared_root=prepared_root, paper_root=paper_root,
                                   live_snapshot=snapshot), **{k: report[k] for k in ("mode", "common_stop", "retrieval_requested")}}
            report["collection_drift"] = {k: snapshot[k] for k in ("extra_members_ignored", "missing_members")}
            mappings, errors = local_mappings(config)
            if errors:
                raise AuditStop("LOCAL_DEFAULT_MAPPING_UNAVAILABLE")
            if verify_retrieval:
                report["formal_bridge_health"] = formal_health(config, mappings, budget, sender=health_sender)
            relevant = {(p["key"], m["dataset_id"], m["document_id"])
                        for p in report["papers"] for m in p["mappings"]}
            selected_maps = [m for m in mappings if any(
                (p["key"], m["source"]["dataset_id"], m["document_id"]) in relevant for p in report["papers"])]
            if selected_maps and api is None:
                key = os.environ.get("RAGFLOW_API_KEY", "")
                if not key:
                    from zotero_scope_processing import runtime_credentials
                    key, _ = runtime_credentials()
                api = bounded_index_api(config, selected_maps, budget,
                    key=key, transport=transport)
            for paper in report["papers"]:
                for mapped in paper["mappings"]:
                    if not mapped["prepared_exact_matches"]:
                        mapped["remote_readback"] = {"status": "not_checked", "reason": "LOCAL_CANDIDATE_MAPPING_DIFFERS"}
                        continue
                    mapping = next(m for m in selected_maps if m["source"]["dataset_id"] == mapped["dataset_id"]
                                   and m["document_id"] == mapped["document_id"])
                    bundle = load_bundle(Path(mapped["prepared_exact_matches"][0]))
                    state_path = state_path_for(Path(mapping["source"]["state_dir"]), api.base,
                                                mapped["dataset_id"], mapped["document_id"])
                    before = sha(state_path)
                    mapped["remote_readback"] = verify_remote_mapping(api, mapping, bundle)
                    if before != sha(state_path):
                        raise AuditStop("IMPORT_STATE_CHANGED_DURING_AUDIT")
                    if mapped["remote_readback"]["status"] != "verified":
                        continue
                    if verify_retrieval and mapped["default_registered"]:
                        token = os.environ.get("RETRIEVAL_API_TOKEN", "")
                        if not token:
                            from zotero_scope_processing import runtime_credentials
                            _, token = runtime_credentials()
                        query = next((p.get("verification_query") for p in scope["papers"] if p["key"] == paper["key"]), None)
                        query = query or ("What findings are reported in " + paper["title"] + "?")
                        mapped["formal_retrieval"] = formal_retrieval(query, mapping, paper["doi"], config, budget,
                            output / "formal-retrieval", token=token,
                            sender=retrieval_sender)
                    verified = [m for m in paper["mappings"] if isinstance(m["remote_readback"], dict)
                                and m["remote_readback"]["status"] == "verified"]
                    if len(verified) == len(paper["mappings"]):
                        paper["resume"] = [x for x in paper["resume"] if x != "remote_readback"]
                    recalls = [m.get("formal_retrieval", {}).get("status") == "verified" for m in paper["mappings"]]
                    if recalls and all(recalls):
                        paper["default_retrieval"] = "verified"
                        paper["resume"] = [x for x in paper["resume"] if x != "formal_default_retrieval"]
        except (AuditStop, ImportError_, OSError, ValueError, KeyError, RuntimeError) as error:
            # Shared identity/transport/quota failures stop remaining calls, but
            # keep the exact-scope eight-row report and completed readbacks.
            report["common_stop"] = str(error)
        finally:
            if api is not None and hasattr(api, "api_key"):
                api.api_key = ""
        final = budget.snapshot()
        report["external_calls"] = len(final["events"]) - len(initial["events"])
        report["batch_reserved_counts"] = final["counts"]
        report["batch_cost_upper_cny"] = final["reserved_cost_upper_cny"]
        report["business_writes"] = 0
        report["summary"].update(remote_verified_papers=sum(bool(p["mappings"]) and all(
            isinstance(m["remote_readback"], dict) and m["remote_readback"]["status"] == "verified"
            for m in p["mappings"]) for p in report["papers"]),
            formal_verified_papers=sum(p["default_retrieval"] == "verified" for p in report["papers"]))
        return report
