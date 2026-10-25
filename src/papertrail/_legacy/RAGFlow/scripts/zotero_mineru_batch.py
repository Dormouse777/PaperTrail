#!/usr/bin/env python3
"""Read one Zotero collection and batch-convert missing PDFs with local MinerU V1 API.

This is intentionally read-only toward Zotero. It writes only generated files and a
provenance manifest under the configured Markdown output root; it never mutates the
Zotero library and never sends PDFs to a cloud endpoint.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath


from workflow_configuration import default_path

ZOTERO_BASE = "http://127.0.0.1:23119/api"
MINERU_BASE = "http://127.0.0.1:8000"


def request(url: str, *, method: str = "GET", data: bytes | None = None,
            headers: dict[str, str] | None = None, timeout: int = 120):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read(), dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:1000]
        raise RuntimeError(f"HTTP {exc.code} {method} {url}: {body}") from exc


def get_json(url: str):
    body, headers = request(url, headers={"Accept": "application/json"})
    return json.loads(body.decode("utf-8")), headers


def post_json(url: str, value: dict):
    body, _ = request(url, method="POST", data=json.dumps(value).encode(),
                      headers={"Content-Type": "application/json", "Accept": "application/json"})
    return json.loads(body.decode("utf-8"))


def pages(url: str):
    while url:
        value, headers = get_json(url)
        yield from value
        link = headers.get("Link", "")
        next_url = None
        for part in link.split(","):
            if 'rel="next"' in part:
                next_url = part[part.find("<") + 1:part.find(">")]
                break
        url = next_url


def local_path_from_file_uri(uri: str) -> Path:
    parts = urllib.parse.urlsplit(uri)
    if parts.scheme.lower() != "file":
        raise ValueError(f"Expected Zotero file: URI, got {uri!r}")
    path = urllib.parse.unquote(parts.path)
    if re.match(r"^/[A-Za-z]:/", path):
        path = path[1:]
    return Path(path)


def collection_candidates(collection_key: str, out_root: Path, include_markdown_children: bool = False):
    collection, _ = get_json(f"{ZOTERO_BASE}/users/0/collections/{collection_key}")
    title = collection.get("data", {}).get("name", collection_key)
    query = urllib.parse.urlencode({"format": "json", "include": "data", "limit": 100})
    items_url = f"{ZOTERO_BASE}/users/0/collections/{collection_key}/items?{query}"
    parents = {}
    for item in pages(items_url):
        data = item.get("data", {})
        if data.get("itemType") != "attachment":
            parents[data.get("key")] = item

    result = []
    for parent_key, parent in parents.items():
        parent_data = parent.get("data", {})
        children_url = f"{ZOTERO_BASE}/users/0/items/{parent_key}/children?format=json&include=data"
        children, _ = get_json(children_url)
        pdfs = [x for x in children if x.get("data", {}).get("contentType") == "application/pdf"]
        has_markdown = any(
            x.get("data", {}).get("contentType") == "text/markdown" for x in children
        )
        for attachment in pdfs:
            attachment_data = attachment.get("data", {})
            attachment_key = attachment_data.get("key")
            filename = attachment_data.get("filename") or attachment_data.get("title") or "source.pdf"
            enclosure = attachment.get("links", {}).get("enclosure", {}).get("href")
            if not attachment_key or not enclosure:
                continue
            pdf = local_path_from_file_uri(enclosure)
            target_dir = out_root / title / attachment_key
            target_md = target_dir / (pdf.stem + ".md")
            if has_markdown and not include_markdown_children:
                state = "skip-zotero-markdown-child"
            elif target_md.is_file() and target_md.stat().st_size > 0:
                state = "skip-existing-output"
            elif not pdf.is_file():
                state = "missing-source-pdf"
            else:
                state = "ready"
            result.append({
                "collectionKey": collection_key,
                "collection": title,
                "parentKey": parent_key,
                "parentTitle": parent_data.get("title", ""),
                "doi": parent_data.get("DOI", ""),
                "parentUrl": parent.get("links", {}).get("alternate", {}).get("href", ""),
                "attachmentKey": attachment_key,
                "pdf": pdf,
                "pdfFilename": filename,
                "targetDir": target_dir,
                "targetMarkdown": target_md,
                "state": state,
            })
    return result


def upload_pdf(pdf: Path):
    content = pdf.read_bytes()
    sha256 = hashlib.sha256(content).hexdigest()
    upload = post_json(f"{MINERU_BASE}/v1/uploads", {
        "filename": pdf.name,
        "bytes": len(content),
        "mime_type": "application/pdf",
        "purpose": "parse",
        "sha256sum": sha256,
    })
    # MinerU may deduplicate an existing upload and immediately return a completed
    # File object; in that case there is no upload URL and a second PUT is invalid.
    if upload.get("status") == "completed" and upload.get("file", {}).get("id"):
        return upload["file"]["id"], sha256
    upload_url = upload.get("upload_url") or f"{MINERU_BASE}/v1/uploads/{upload['id']}/content"
    request(upload_url, method=upload.get("upload_method") or "PUT", data=content,
            headers=upload.get("upload_headers") or {"Content-Type": "application/pdf"}, timeout=900)
    completed = post_json(f"{MINERU_BASE}/v1/uploads/{upload['id']}/complete", {"sha256sum": sha256})
    file_id = completed.get("file", {}).get("id") or completed.get("id")
    if not file_id:
        raise RuntimeError(f"MinerU upload completion returned no file ID for {pdf.name}")
    return file_id, sha256


def parse_batch(candidates, tier: str, timeout_minutes: int):
    file_ids = []
    for item in candidates:
        file_id, sha256 = upload_pdf(item["pdf"])
        item["sourcePdfSha256"] = sha256
        item["mineruFileId"] = file_id
        file_ids.append({"source": {"type": "file_id", "file_id": file_id}})
    job = post_json(f"{MINERU_BASE}/v1/parse/jobs", {
        "files": file_ids,
        "tier": tier,
        "ocr_mode": "auto",
        "output_formats": ["zip"],
    })
    deadline = time.monotonic() + timeout_minutes * 60
    while time.monotonic() < deadline:
        status, _ = get_json(f"{MINERU_BASE}/v1/parse/jobs/{job['job_id']}")
        if status["status"] in {"completed", "partial", "failed", "canceled"}:
            return status
        time.sleep(2)
    raise TimeoutError(f"MinerU job {job['job_id']} exceeded {timeout_minutes} minutes")


def download_output(file_id: str, dest: Path):
    data, _ = request(f"{MINERU_BASE}/v1/files/{file_id}/content", timeout=900)
    dest.write_bytes(data)


def safe_extract_bundle(bundle: Path, stage: Path):
    with zipfile.ZipFile(bundle) as archive:
        names = archive.namelist()
        for name in names:
            member = PurePosixPath(name)
            if member.is_absolute() or ".." in member.parts:
                raise ValueError(f"Unsafe path in MinerU ZIP: {name}")
        md_members = [n for n in names if PurePosixPath(n).name == "markdown.md"]
        json_members = [n for n in names if PurePosixPath(n).name == "middle_json.json"]
        if len(md_members) != 1 or len(json_members) != 1:
            raise ValueError("MinerU ZIP must contain exactly one markdown.md and middle_json.json")
        md_name, json_name = md_members[0], json_members[0]
        md = archive.read(md_name).decode("utf-8-sig")
        middle = archive.read(json_name)
        if not md.strip() or not re.search(r"(?m)^#{1,6}\s+", md):
            raise ValueError("MinerU Markdown validation failed")
        image_members = {}
        for name in names:
            posix = PurePosixPath(name)
            if "images" in posix.parts and not name.endswith("/"):
                idx = posix.parts.index("images")
                rel = PurePosixPath(*posix.parts[idx + 1:])
                if rel.parts:
                    image_members[str(rel)] = archive.read(name)
        refs = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", md)
        missing = [ref for ref in refs if not re.match(r"^(?:https?:|data:|#|mailto:)", ref)
                   and ref.startswith("images/") and ref[7:] not in image_members]
        if missing:
            raise ValueError(f"MinerU Markdown has missing image assets: {missing[:5]}")
        stage.mkdir(parents=True, exist_ok=True)
        (stage / "markdown.md").write_text(md, encoding="utf-8")
        (stage / "middle_json.json").write_bytes(middle)
        for rel, content in image_members.items():
            target = stage / "images" / Path(*PurePosixPath(rel).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        headings = list(dict.fromkeys(
            m.group(1).strip() for m in re.finditer(r"(?m)^#{1,6}\s+(.+?)\s*$", md)
        ))
        return md, headings, len(refs), len(image_members)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection", default="RRRN23K5", help="Zotero collection key")
    parser.add_argument("--output-root", type=Path, default=default_path("bundle_root", "data/bundles"))
    parser.add_argument("--limit", type=int, default=2, help="maximum PDFs in this run")
    parser.add_argument("--include-existing-markdown", action="store_true",
                        help="also convert PDFs whose Zotero parent already has a Markdown child")
    parser.add_argument("--tier", choices=["flash", "basic", "standard", "advanced"], default="standard")
    parser.add_argument("--timeout-minutes", type=int, default=90)
    parser.add_argument("--execute", action="store_true", help="submit missing PDFs to local MinerU")
    args = parser.parse_args()

    health, _ = get_json(f"{MINERU_BASE}/v1/health")
    candidates = collection_candidates(args.collection, args.output_root, args.include_existing_markdown)
    ready = [x for x in candidates if x["state"] == "ready"][:args.limit]
    state_counts = {}
    for item in candidates:
        state_counts[item["state"]] = state_counts.get(item["state"], 0) + 1
    print(json.dumps({"mineruVersion": health.get("version"), "collectionKey": args.collection,
                      "totalPdfs": len(candidates), "stateCounts": state_counts,
                      "readyCount": sum(x["state"] == "ready" for x in candidates),
                      "selected": [{"parentKey": x["parentKey"], "attachmentKey": x["attachmentKey"],
                                    "title": x["parentTitle"], "pdf": str(x["pdf"]), "state": x["state"]}
                                   for x in ready],
                      "skippedMarkdownChildren": [{"parentKey": x["parentKey"], "attachmentKey": x["attachmentKey"],
                                                   "title": x["parentTitle"]}
                                                  for x in candidates if x["state"] == "skip-zotero-markdown-child"]},
                     ensure_ascii=False, indent=2))
    if not args.execute:
        print("Dry run only. Add --execute to upload the selected PDFs to the local MinerU API.")
        return 0
    if not ready:
        print("No eligible PDFs to convert.")
        return 0
    job = parse_batch(ready, args.tier, args.timeout_minutes)
    job_files = job.get("files", [])
    by_name = {x.get("name"): x for x in job_files}
    errors = []
    for item in ready:
        result = by_name.get(item["pdfFilename"])
        if result is None:
            result = next((x for x in job_files if x.get("file_id") == item["mineruFileId"]), None)
        if not result or result.get("status") != "completed":
            errors.append({"attachmentKey": item["attachmentKey"], "error": result or "missing job result"})
            continue
        zip_ref = (result.get("output_files") or {}).get("zip") or {}
        if not zip_ref.get("file_id"):
            errors.append({"attachmentKey": item["attachmentKey"], "error": "No ZIP output"})
            continue
        item["zipFileId"] = zip_ref["file_id"]

    for item in ready:
        if not item.get("zipFileId"):
            continue
        target_dir: Path = item["targetDir"]
        if target_dir.exists():
            errors.append({"attachmentKey": item["attachmentKey"], "error": f"Refusing existing directory: {target_dir}"})
            continue
        # The first conversion for a collection has no output parent yet. Create it
        # before downloading the completed ZIP, because download_output writes directly
        # to this path.
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        stage = target_dir.parent / ("." + item["attachmentKey"] + ".staging")
        bundle = stage.parent / (item["attachmentKey"] + ".mineru.zip")
        try:
            download_output(item["zipFileId"], bundle)
            md, headings, refs, images = safe_extract_bundle(bundle, stage)
            stage_md = stage / "markdown.md"
            final_md = stage / (item["pdf"].stem + ".md")
            stage_md.replace(final_md)
            manifest = {
                "title": item["parentTitle"], "DOI": item["doi"],
                "collectionKey": item["collectionKey"],
                "zoteroCollectionKey": item["collectionKey"],
                "zoteroParentKey": item["parentKey"],
                "zoteroAttachmentKey": item["attachmentKey"],
                "zoteroPdfAttachmentKey": item["attachmentKey"],
                "zoteroParentUrl": item["parentUrl"],
                "zoteroParentLocalUri": f"zotero://select/library/items/{item['parentKey']}",
                "zoteroPdfAttachmentLocalUri": f"zotero://select/library/items/{item['attachmentKey']}",
                "sourcePdf": str(item["pdf"]), "sourcePdfFilename": item["pdfFilename"],
                "sourcePdfSha256": item["sourcePdfSha256"],
                "markdown": str(target_dir / final_md.name),
                "mineruVersion": health.get("version"), "minerUVersion": health.get("version"),
                "mineruJobId": job["job_id"],
                "sections": headings, "imageReferenceCount": refs,
                "imageAssetCount": images,
            }
            (stage / "provenance.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            stage.replace(target_dir)
            print(f"converted {item['attachmentKey']}: {target_dir / final_md.name}; headings={len(headings)} images={images}")
        except Exception as exc:
            errors.append({"attachmentKey": item["attachmentKey"], "error": str(exc)})
        finally:
            bundle.unlink(missing_ok=True)
    print(json.dumps({"jobId": job.get("job_id"), "jobStatus": job.get("status"), "errors": errors},
                     ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
