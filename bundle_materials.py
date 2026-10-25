"""Continue independent materials using the existing bundle and import states."""

import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Paperdownload"))
from paper_bundle import file_identity, read_bundle, write_bundle, _regular_chain
from paper_supplements import _valid_license
from workflow_configuration import DEFAULT_CONFIGURATION, read_configuration, configuration_path
from docx_methods_source import chunk_docx_methods, native_supporting_document
from local_pdf_pipeline import run_pipeline
from ragflow_prechunk_import import ImportError_, load_bundle, reconcile, state_path_for, text_digest, paper_metadata
from ragflow_retrieve import load_mapping


def material_index_target(role, state_dir, base_url, dataset_id, *, configuration=None, previous=None):
    """Route new attachments; resume existing writes in their original scope."""
    target = {"state_dir": str(state_dir), "base_url": base_url.rstrip("/"), "dataset_id": dataset_id}
    explicit_configuration = configuration is not None
    configuration = Path(configuration) if explicit_configuration else DEFAULT_CONFIGURATION
    config = read_configuration(configuration) if explicit_configuration or configuration.is_file() else {}
    primary = config.get("material_targets", {}).get("main_pdf")
    if not primary or (primary.get("dataset_id") != dataset_id
            or primary.get("base_url") != base_url.rstrip("/")
            or Path(primary.get("state_dir", "")).resolve() != Path(state_dir).resolve()):
        return target
    if role != "main_pdf":
        target = config.get("material_targets", {}).get("supplement")
        if not target:
            raise ValueError("SUPPLEMENT_TARGET_NOT_CONFIGURED")
    if previous and previous.get("dataset_id"):
        matches = [source for source in config.get("sources", [])
                   if source.get("dataset_id") == previous["dataset_id"]
                   and source.get("base_url") == previous.get("base_url")]
        if len(matches) != 1:
            raise ValueError("MATERIAL_PREVIOUS_TARGET_UNCONFIRMED")
        target = {key: matches[0][key] for key in ("state_dir", "base_url", "dataset_id")}
    if not any(source.get("confirmed") is True and all(source.get(k) == target[k] for k in target)
               for source in config.get("sources", [])):
        raise ValueError("MATERIAL_TARGET_UNCONFIRMED")
    if target["base_url"] != base_url.rstrip("/"):
        raise ValueError("MATERIAL_TARGET_ENDPOINT_CHANGED")
    return target


def _register_verified_material(folder, manifest, material, rows, state_dir, base_url, dataset_id, *, configuration=None):
    configuration = (Path(configuration) if configuration is not None else
                     DEFAULT_CONFIGURATION)
    if not configuration.is_file():
        return
    from process_lock import process_lock
    with process_lock(configuration.with_suffix(".lock")):
        config = json.loads(configuration.read_text(encoding="utf-8"))
        if config.get("scope") != "unified" or not any(source.get("confirmed") is True
                and source.get("dataset_id") == dataset_id and source.get("base_url") == base_url.rstrip("/")
                and configuration_path(source.get("state_dir", ""), configuration) == Path(state_dir).resolve()
                for source in config.get("sources", [])):
            return
        before = json.dumps(config, sort_keys=True)
        paths = config.setdefault("bundle_paths", [])
        path = str(Path(folder).resolve() / "bundle.json")
        if not any(Path(value).resolve() == Path(path).resolve() for value in paths):
            paths.append(path)
        if rows["provenance"].get("source_pdf_sha256") or rows["provenance"].get("source_docx_sha256"):
            title = manifest["paper"]["title"]
            if material["role"] != "main_pdf":
                title += " [Supporting Information: " + material["filename"] + "]"
            overlay = {"source_pdf_sha256": rows["provenance"].get("source_pdf_sha256"),
                       "title": title, "doi": manifest["paper"]["doi"]}
            registry = config.setdefault("source_registry", {})
            previous = registry.get(rows["document_id"])
            if previous is not None and previous != overlay:
                raise ValueError("MATERIAL_BIBLIOGRAPHY_CONFLICT")
            registry[rows["document_id"]] = overlay
        if before != json.dumps(config, sort_keys=True):
            temporary = configuration.with_suffix(".material.tmp")
            temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(configuration)


def _visible_failure(reason):
    if "ZOTERO" in reason:
        return "归档尚未完成或未通过核验"
    if "READBACK" in reason or "INDEX" in reason:
        return "索引读回尚未通过核验，等待对账续办"
    if "SOURCE" in reason or "CHANGED" in reason:
        return "原件或来源记录不一致，等待核对"
    if "DOCX" in reason:
        return "附件含当前不支持或尚未通过核验的Word结构"
    return "材料处理失败，等待从记录的失败阶段续办"


def _write_derived(path, value):
    raw = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError("MATERIAL_DERIVED_CONFLICT")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


def _filed(manifest, material):
    zotero = manifest.get("zotero", {})
    rows = ([zotero.get("pdf", {})] if material["role"] == "main_pdf" else
            zotero.get("supplements", {}).get("files", []))
    matches = [row for row in rows if row.get("sha256") == material["sha256"]
               and row.get("status") == "FILED"
               and (material["role"] == "main_pdf" or row.get("relative_path") == material["relative_path"])]
    if len(matches) != 1 or not zotero.get("parent_key") or not matches[0].get("attachment_key"):
        raise ValueError("MATERIAL_ZOTERO_NOT_VERIFIED")
    return zotero["parent_key"], matches[0]["attachment_key"]


def _mapping_for(state_dir, api, dataset_id, rows):
    path = state_path_for(state_dir, api.base, dataset_id, rows["document_id"])
    if not path.is_file():
        return None
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("import_in_progress") is not False or state.get("completed_source_sha256") != rows["sha256"]:
        return None
    documents, mapping = load_mapping(state_dir, api.base, dataset_id, skip_incomplete=True)
    document_id = state["ragflow_document_id"]
    document = api.find_document(dataset_id, rows["document_name"])
    if (document is None or document.get("id") != document_id or document.get("type") != "virtual"
            or any((document.get("meta_fields") or {}).get(k) != v for k, v in paper_metadata(rows).items())):
        raise ImportError_("MATERIAL_INDEX_METADATA_READBACK_DIFFERS")
    expected = {key: value["record"]["embedding_text"] for key, value in mapping.items()
                if value["ragflow_document_id"] == document_id}
    response = api.list_chunks(dataset_id, document_id)
    actual = {row["id"]: row["content"] for row in response["chunks"]}
    if (document_id not in documents or actual != expected or response["total"] != len(expected)
            or (response.get("doc") or {}).get("chunk_count") != len(expected)
            or document.get("chunk_count") != len(expected)):
        raise ImportError_("MATERIAL_INDEX_READBACK_DIFFERS")
    return {"document_id": document_id, "mapping_path": str(path), "chunks": len(expected), "added": 0}


def _prepare_docx(folder, manifest, material, prepared_root, tokenizer_json, max_tokens):
    parent, attachment = _filed(manifest, material)
    paper = manifest["paper"]
    main = next(row for row in manifest["files"] if row["role"] == "main_pdf")
    source = folder / material["relative_path"]
    with zipfile.ZipFile(source) as archive:
        if archive.getinfo("word/document.xml").file_size > 16 * 1024 * 1024:
            raise ValueError("DOCX_DOCUMENT_XML_TOO_LARGE")
        xml = archive.read("word/document.xml")
    target = prepared_root / material["sha256"][:20] / ("native-" + text_digest(str(tokenizer_json) + str(max_tokens))[:12])
    binding = {"source_sha256": material["sha256"],
        "word_document_xml_sha256": hashlib.sha256(xml).hexdigest(),
        "parent_pdf_project_document_id": main["sha256"][:20],
        "supplement_source_id": material["sha256"][:20],
        "source_role": "supporting_information", "source_url": material["source_url"],
        "doi": paper["doi"], "pmcid": paper.get("pmcid", ""), "pmid": paper.get("id", ""),
        "source_title": paper["title"]}
    binding_path = target / "source-binding.json"
    _write_derived(binding_path, binding)
    canonical = native_supporting_document(source, binding_path,
        zotero_parent_key=parent, zotero_attachment_key=attachment)
    canonical_path = target / "canonical.json"
    _write_derived(canonical_path, canonical)
    rows = chunk_docx_methods(canonical_path, tokenizer_json, max_tokens=max_tokens)
    path = target / "chunks.jsonl"
    raw = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    if path.exists() and path.read_text(encoding="utf-8") != raw:
        raise ValueError("MATERIAL_CHUNKS_CHANGED")
    if not path.exists():
        path.write_text(raw, encoding="utf-8")
    return path, {"image_content": "captions_only" if canonical["provenance"]["unparsed_image_body_blocks"] else "not_present",
                  "unparsed_image_count": len(canonical["provenance"]["unparsed_image_body_blocks"])}


def _bound_source(manifest, material):
    material = dict(material)
    if not material.get("source_url"):
        results = [row for row in manifest.get("supplements", {}).get("results", [])
            if row.get("sha256") == material["sha256"]
            and row.get("relative_path") == material["relative_path"]
            and row.get("status") in ("exists", "downloaded") and row.get("request_url")]
        urls = {row["request_url"] for row in results}
        if len(urls) != 1:
            raise ValueError("MATERIAL_OFFICIAL_SOURCE_NOT_BOUND")
        material["source_url"] = next(iter(urls))
    if not _valid_license(material.get("license")) or not material["source_url"].startswith("https://"):
        raise ValueError("MATERIAL_SOURCE_PERMISSION_NOT_VERIFIED")
    return material


def continue_materials(pdf, prepared_root, bundle_root, state_dir, base_url, dataset_id,
                       *, execute_local=False, execute_ragflow=False, api=None,
                       tokenizer_json=None, main_chunks_path=None, max_tokens=2000,
                       relative_paths=None, api_factory=None, configuration=None, target_api_factory=None):
    pdf, prepared_root = Path(pdf), Path(prepared_root)
    manifest = read_bundle(pdf.parent)
    if manifest is None:
        return {"materials": [], "status": "bundle_missing", "failures": []}
    mains = [item for item in manifest["files"] if item.get("role") == "main_pdf"]
    if (len(mains) != 1 or mains[0]["relative_path"] != pdf.name
            or file_identity(pdf)["sha256"] != mains[0]["sha256"]):
        raise ValueError("BUNDLE_MAIN_PDF_CHANGED")
    seen = set()
    for material in manifest["files"]:
        relative = material.get("relative_path")
        if (not isinstance(relative, str) or "\\" in relative or ":" in relative
                or PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts
                or relative.casefold() in seen
                or material.get("role") not in ("main_pdf", "supplement")
                or material["role"] == "supplement" and (len(PurePosixPath(relative).parts) != 2
                    or PurePosixPath(relative).parts[0] != "supplements")):
            raise ValueError("MATERIAL_PATH_INVALID")
        seen.add(relative.casefold())
        _regular_chain(pdf.parent / relative)
    processing = manifest.setdefault("processing", {})
    existing = {row["relative_path"]: row for row in processing.get("materials", [])}
    selected = set(relative_paths) if relative_paths is not None else {m["relative_path"] for m in manifest["files"]}
    if not selected <= {m["relative_path"] for m in manifest["files"]}:
        raise ValueError("MATERIAL_SCOPE_DIFFERS")
    reports, failures = [], []
    uncertain = False
    for material in manifest["files"]:
        relative = material["relative_path"]
        if relative not in selected:
            continue
        record = dict(existing.get(relative, {}))
        if record.get("sha256") not in (None, material["sha256"]):
            raise ValueError("MATERIAL_SOURCE_VERSION_CHANGED")
        record.update({"relative_path": relative, "sha256": material["sha256"],
                       "role": material["role"], "filename": material["filename"]})
        source = pdf.parent / relative
        stage = "source"
        attempted = False
        try:
            if file_identity(source)["sha256"] != material["sha256"]:
                raise ValueError("MATERIAL_SOURCE_CHANGED")
            extension = source.suffix.casefold()
            if material["role"] != "main_pdf" and extension not in (".pdf", ".docx"):
                try:
                    _filed(manifest, material)
                    archived = "FILED"
                except ValueError:
                    archived = "pending"
                record.update({"status": "unsupported", "resume_stage": "parse", "archive_status": archived,
                               "reason": "当前解析路径不支持此格式；原件已保留"})
                reports.append(record)
                continue
            stage = "archive"
            parent, attachment = _filed(manifest, material)
            record.update({"archive_status": "FILED", "zotero_parent_key": parent,
                           "zotero_attachment_key": attachment})
            if material["role"] == "main_pdf":
                path = Path(main_chunks_path) if main_chunks_path else None
                if path is None:
                    record.update({"status": "pending", "resume_stage": "prepare_main",
                                   "reason": "正文等待现有准备流程"})
                    reports.append(record)
                    continue
            else:
                material = _bound_source(manifest, material)
                path = Path(record["chunks_path"]) if record.get("chunks_path") else None
            stage = "parse"
            if path is None or not path.is_file():
                if not execute_local:
                    record.update({"status": "pending", "resume_stage": "parse"})
                    reports.append(record)
                    continue
                if extension == ".docx":
                    if tokenizer_json is None:
                        raise ValueError("DOCX_LOCAL_TOKENIZER_REQUIRED")
                    path, image_status = _prepare_docx(pdf.parent, manifest, material, prepared_root,
                                                      tokenizer_json, max_tokens)
                    record.update(image_status)
                else:
                    result = run_pipeline(source, bundle_root, prepared_root, execute=True,
                                          tokenizer_json=tokenizer_json)
                    path = Path(result["preparation"]["chunks_path"])
                    native_rows = load_bundle(path)["rows"]
                    for native_row in native_rows:
                        native_row["provenance"].update({"source_role": "supporting_information",
                            "parent_document_id": mains[0]["sha256"][:20], "doi": manifest["paper"]["doi"],
                            "attachment_name": material["filename"], "zotero_parent_key": parent,
                            "zotero_attachment_key": attachment, "source_url": material.get("source_url")})
                    path = path.parent / "material-chunks.jsonl"
                    path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in native_rows), encoding="utf-8")
                    record.update({"ocr_review_required": True, "image_content": "unverified"})
            stage = "index"
            rows = load_bundle(path)
            provenance = rows["provenance"]
            if ((provenance.get("source_docx_sha256") or provenance.get("source_pdf_sha256")) != material["sha256"]
                    or record.get("chunks_sha256") not in (None, rows["sha256"])):
                raise ValueError("MATERIAL_PREPARED_SOURCE_DIFFERS")
            record.update({"status": "prepared", "chunks_path": str(path), "chunks_sha256": rows["sha256"],
                             "source_document_id": rows["document_id"], "resume_stage": "index", "reason_code": None})
            if extension == ".pdf" and material["role"] != "main_pdf":
                review = material.get("text_review", {})
                if review.get("source_sha256") != material["sha256"] or review.get("verified") is not True:
                    record.update({"status": "needs_review", "resume_stage": "review",
                                     "reason": "PDF解析文本尚未核实，不发布OCR科研证据", "reason_code": "PDF_TEXT_REVIEW_REQUIRED"})
                    reports.append(record)
                    continue
                record.update({"ocr_review_required": False, "text_verified": True})
            if execute_ragflow and not uncertain:
                target = material_index_target(material["role"], state_dir, base_url, dataset_id,
                                               configuration=configuration, previous=record)
                target_state = Path(target["state_dir"])
                target_dataset = target["dataset_id"]
                if target_api_factory is not None:
                    api = target_api_factory(rows, target)
                elif api_factory is not None:
                    api = api_factory(rows)
                if api is None or api.base != base_url.rstrip("/"):
                    raise ValueError("MATERIAL_INDEX_CLIENT_REQUIRED")
                result = _mapping_for(target_state, api, target_dataset, rows)
                if result is None:
                    # Persist destination before dispatch so an unknown write is
                    # reconciled in that same dataset even after config changes.
                    record.update(dataset_id=target_dataset, base_url=base_url.rstrip("/"))
                    processing["materials"] = reports + [record] + [r for p,r in existing.items()
                        if p not in {x["relative_path"] for x in reports} and p != relative]
                    write_bundle(pdf.parent, manifest)
                    attempted = True
                    result = reconcile(api, target_dataset, rows,
                        state_path_for(target_state, api.base, target_dataset, rows["document_id"]))
                record.update({"status": "searchable", "resume_stage": None, "reason": None,
                    "reason_code": None, "write_uncertain": False,
                    "dataset_id": target_dataset, "base_url": base_url.rstrip("/"),
                    "ragflow_document_id": result["document_id"], "chunk_count": result["chunks"],
                    "mapping_path": result["mapping_path"], "index_readback_verified": True})
                _register_verified_material(pdf.parent, manifest, material, rows, target_state, base_url, target_dataset,
                                            configuration=configuration)
        except (ValueError, OSError, KeyError, RuntimeError, zipfile.BadZipFile) as error:
            uncertain = uncertain or attempted
            record.update({"status": "failed", "resume_stage": stage, "index_readback_verified": False,
                           "reason": _visible_failure(str(error)), "reason_code": str(error), "write_uncertain": attempted})
            failures.append({"relative_path": relative, "stage": stage, "reason": str(error),
                             "write_uncertain": attempted})
        reports.append(record)
        if execute_local or execute_ragflow:
            processing.update({"materials": reports + [row for key, row in existing.items()
                if key not in {item["relative_path"] for item in reports}],
                "dataset_id": dataset_id, "canonical_api_base": base_url.rstrip("/")})
            write_bundle(pdf.parent, manifest)
    if execute_local or execute_ragflow:
        processing["materials"] = reports + [r for p, r in existing.items() if p not in selected]
        write_bundle(pdf.parent, manifest)
    return {"materials": reports, "failures": failures, "write_uncertain": uncertain,
            "status": "complete" if reports and all(row.get("status") == "searchable" for row in reports) else "partial"}


def material_overview(bundle_path, mappings):
    folder = Path(bundle_path).parent
    manifest = read_bundle(folder)
    if manifest is None:
        raise ValueError("MATERIAL_BUNDLE_MISSING")
    processing = manifest.get("processing", {})
    records = {row["relative_path"]: row for row in processing.get("materials", [])}
    output = []
    for material in manifest["files"]:
        record = records.get(material["relative_path"], {})
        source_hash = material["sha256"]
        matches = [entry["record"] for entry in mappings.values()
            if source_hash in (entry["record"]["provenance"].get("source_pdf_sha256"),
                               entry["record"]["provenance"].get("source_docx_sha256"))]
        current = file_identity(folder / material["relative_path"])["sha256"] == source_hash
        searchable = bool(matches) and current
        try:
            _filed(manifest, material)
            archived = True
        except ValueError:
            archived = False
        images = record.get("image_content")
        if matches and images is None:
            provenance = matches[0]["provenance"]
            canonical = provenance.get("source_canonical")
            if canonical:
                document = json.loads(Path(canonical).read_text(encoding="utf-8"))
                if document.get("provenance", {}).get("unparsed_image_body_blocks"):
                    images = "captions_only"
        parsed = bool(matches)
        if not parsed and record.get("chunks_path"):
            try:
                prepared = load_bundle(Path(record["chunks_path"]))
                parsed = prepared["sha256"] == record.get("chunks_sha256")
            except (ImportError_, OSError, ValueError):
                parsed = False
        output.append({"name": material["filename"], "role": material["role"],
            "source_sha256": source_hash,
            "searchable": searchable, "archived": archived, "parsed": parsed,
            "image_content": images or "not_checked",
            "reason": (record.get("reason") or "等待解析或索引核验") if not searchable else None})
    supplements = manifest.get("supplements", {})
    return {"doi": manifest["paper"].get("doi"), "title": manifest["paper"].get("title"),
            "official_supplements_status": supplements.get("status", "not_checked"),
            "acquisition_results": [{"name": row.get("filename") or row.get("resource_label") or "未取得附件（文件名未知）", "status": row.get("status"),
                                     "reason": row.get("reason_code") or row.get("error")} for row in supplements.get("results", [])],
            "materials": output}


def public_material_summary(status):
    materials = status.get("materials", [])
    main = next((item for item in materials if item["role"] == "main_pdf"), None)
    supplements = [item for item in materials if item["role"] != "main_pdf"]
    official = status.get("official_supplements_status", "not_checked")
    labels = {"complete": "已核对并取得", "none": "已核对无补件",
              "partial": "取得不完整", "not_checked": "尚未核对"}
    parts = ["正文" + ("可检索" if main and main["searchable"] else "检索状态未确认"),
             "官方补件" + labels.get(official, "待核对/取得")]
    if supplements:
        parts.append("附件解析" + str(sum(item["parsed"] for item in supplements)) + "/" + str(len(supplements))
                     + "，可检索" + str(sum(item["searchable"] for item in supplements)) + "/" + str(len(supplements)))
    if any(item.get("image_content") == "captions_only" for item in materials):
        parts.append("图像仅有图注，图内内容未核实")
    elif any(item.get("image_content") in ("unverified", "not_checked") for item in materials):
        parts.append("图像内容核实状态未知")
    missing = [item["name"] + "（" + (item["reason"] or "等待核验") + "）"
               for item in materials if not item["searchable"]]
    missing.extend((item.get("name") or "未取得附件（文件名未知）") + "（" + (item.get("reason") or item.get("status") or "待核对") + "）"
                   for item in status.get("acquisition_results", [])
                   if item.get("status") not in ("exists", "downloaded", "saved", "SAVED", "complete"))
    if missing:
        parts.append("待办：" + "、".join(missing))
    return "；".join(parts)


def attach_material_status(response, config_path, api):
    from unified_retrieval import load_sources
    config = load_sources(config_path)
    mappings = {}
    for source in config["sources"]:
        try:
            _, mapping = load_mapping(Path(source["state_dir"]), api.base, source["dataset_id"], skip_incomplete=True)
            mappings.update({(source["dataset_id"], key): value for key, value in mapping.items()})
        except (ImportError_, OSError, ValueError):
            continue
    paths = {Path(value) for value in config.get("bundle_paths", [])}
    for root in config.get("bundle_roots", []):
        paths.update(Path(root).glob("*/bundle.json"))
    statuses = []
    for path in sorted(paths):
        try:
            statuses.append(material_overview(path, mappings))
        except (ValueError, OSError, KeyError):
            statuses.append({"doi": None, "title": path.parent.name, "status": "records_unavailable"})
    response["material_status"] = statuses
    question = response.get("query", "").casefold()
    for result in response.get("results", []):
        source_hash = result.get("source_docx_sha256") or result.get("source_pdf_sha256")
        selected = next((item for item in statuses if item.get("doi") and
                         item["doi"].casefold() == (result.get("doi") or "").casefold()
                         and (source_hash is None or any(material.get("source_sha256") == source_hash
                              for material in item.get("materials", [])))), None)
        result["material_status"] = selected or {"official_supplements_status": "not_checked"}
        result["material_summary"] = public_material_summary(selected) if selected else "正文已索引；官方补充材料尚未核对，附件及图像核实状态未知"
        notices = []
        if selected:
            image_query = any(word in question for word in ("figure", "fig.", "图", "图像", "image"))
            supplement_query = any(word in question for word in ("supplement", "补充", "附件", "table s", "figure s"))
            if image_query and any(item["image_content"] in ("captions_only", "unverified") for item in selected.get("materials", [])):
                notices.append("本篇图像内容尚未完成可靠核实；可检索图注不能用于提供图中精确数值。")
            if supplement_query and selected.get("official_supplements_status") not in ("complete", "none"):
                notices.append("本篇官方补充材料尚未完成核对或取得，本次补充证据可能不完整。")
            for item in selected.get("materials", []):
                if supplement_query and item["role"] != "main_pdf" and not item["searchable"]:
                    notices.append(item["name"] + "尚不可检索：" + (item["reason"] or "等待解析或索引核验"))
        elif any(word in question for word in ("supplement", "补充", "附件")):
            notices.append("该篇补充材料尚未核对，不能据此认定没有补充材料。")
        result["material_notice"] = " ".join(notices)
    return response
