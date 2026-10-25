#!/usr/bin/env python3
"""根据关键字自动搜索并下载论文的 CLI 工具，供 agent 通过 subprocess 自动调用。

数据源：
  - arXiv（计算机/物理/数学等预印本，可直接下载 PDF）
  - PubMed（NCBI E-utilities，生物医学，尽量下载 PMC 开放获取 PDF）

用法示例：
  # 搜索（输出 JSON，便于 agent 解析）
  python3 paper_downloader.py search --query "large language model" --source arxiv --max 5 --json

  # 搜索并下载
  python3 paper_downloader.py download --query "attention is all you need" --source arxiv --max 3 --outdir ./downloads --json

  # 同时搜两个源
  python3 paper_downloader.py download --query "cancer immunotherapy" --source all --max 10 --outdir ./downloads --json

依赖：仅使用 Python 标准库，无需 pip 安装第三方包。

输出契约（供 agent 解析）：
  --json 时始终输出单个 JSON 对象：
    {"query": ..., "source": ..., "count": N,
     "papers": [ {source,id,pmcid,pmc_article_url,title,authors,summary,published,url,pdf_available,(pdf_path,status)} ],
     "errors": [ {"source": ..., "error": ...} ]}
  PDF URLs are used internally but never printed: publisher links can contain
  short-lived credentials in a query or path.
  退出码：0=成功；1=所有数据源均失败；2=部分失败（部分源失败或部分 PDF 下载失败）。

限速说明：
  - arXiv 官方要求每 3 秒最多 1 次 API 请求，超限返回 HTTP 406，脚本已内置限速与退避重试。
  - NCBI 无 API key 时上限约 3 请求/秒；本工具默认每域更保守。
    如需 API key，仅通过 NCBI_API_KEY 环境变量提供，不写在命令行。
"""

import argparse
import hashlib
import json
import os
import re
import ssl
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

from download_guard import GuardError, RequestGovernor, safe_download_diagnostics
from download_jobs import DownloadJobStore
from download_plan import PlanError, load_plan
from download_ledger import DownloadLedger, LedgerConflict, MIN_PDF_BYTES, normalized_doi, sanitized_source_page, verify_pdf
from process_lock import DownloadInUse, process_lock
from fulltext_url import europepmc_pdf_api_url
from paper_bundle import DEFAULT_PAPER_DIRECTORY, BundleError, ensure_bundle, finish_download
from zotero_preflight import (InvalidCandidateError, ZoteroLocalApi,
                              compare_with_zotero, preflight_paper)

ARXIV_API = "https://export.arxiv.org/api/query"
NCBI_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
PMC_PDF = "https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/pdf/"
EUROPE_PMC_API = "https://www.ebi.ac.uk/europepmc/webservices/rest"
PMC_AWS_BUCKET = "https://pmc-oa-opendata.s3.amazonaws.com"

USER_AGENT = "paper-downloader/1.0 (agent automation script)"

# NCBI 限速（无 API key 时 3 req/s，留一点余量）
NCBI_INTERVAL = 0.35

# arXiv 官方要求每 3 秒最多 1 次 API 请求；超限会返回 406
ARXIV_INTERVAL = float(os.environ.get("ARXIV_INTERVAL", "3.0"))
# 对 406/429/5xx 做指数退避重试（406 通常是 arXiv 限流，需要等更久）
RETRY_BACKOFF = 5.0
RETRIES = 3


def _make_ssl_context():
    """Use Windows' trusted root store while keeping TLS verification enabled."""
    context = ssl.create_default_context()
    if os.name == "nt" and hasattr(ssl, "enum_certificates"):
        roots = [
            ssl.DER_cert_to_PEM_cert(cert)
            for cert, encoding, _trust in ssl.enum_certificates("ROOT")
            if encoding == "x509_asn"
        ]
        if roots:
            context.load_verify_locations(cadata="".join(roots))
    return context


SSL_CONTEXT = _make_ssl_context()


class GovernedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, governor):
        self.governor = governor

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if req.type == "https" and urllib.parse.urlsplit(newurl).scheme != "https":
            raise ValueError("Refusing HTTPS-to-HTTP downgrade on redirect")
        self.governor.acquire(newurl)  # redirects are requests, including login-page redirects
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class PaperDownloader:
    def __init__(self, email=None, tool="paper_downloader", api_key=None,
                 state_path=None, governor=None):
        self.email = email
        self.tool = tool
        self.api_key = api_key
        self._last_ncbi = 0.0
        self._last_arxiv = 0.0
        self.governor = governor or RequestGovernor(
            Path(state_path) if state_path else Path(__file__).with_name("download_state.json"))
        # The fixed installation-wide lock also protects callers that import
        # PaperDownloader directly instead of going through main().
        self._process_lock_path = Path(__file__).with_name("paper_downloader-global")
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=SSL_CONTEXT), GovernedRedirectHandler(self.governor))

    # ---------- 通用 HTTP ----------
    def _get_bytes(self, url, headers=None, timeout=30, retries=RETRIES, download_attempt=False,
                   authenticated=False):
        # Legacy name: retries is the total attempt budget, including the
        # first request. Validate before acquiring a lock or updating limits.
        if type(retries) is not int or not 1 <= retries <= RETRIES:
            raise ValueError("HTTP_ATTEMPT_BUDGET_INVALID")
        if type(authenticated) is not bool:
            raise ValueError("authenticated must be a boolean")
        with process_lock(self._process_lock_path):
            return self._get_bytes_locked(url, headers=headers, timeout=timeout, retries=retries,
                                          download_attempt=download_attempt, authenticated=authenticated)

    def _get_bytes_locked(self, url, headers=None, timeout=30, retries=RETRIES, download_attempt=False,
                          authenticated=False):
        if type(retries) is not int or not 1 <= retries <= RETRIES:
            raise ValueError("HTTP_ATTEMPT_BUDGET_INVALID")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
        last_exc = None
        for attempt in range(retries):
            self.governor.acquire(url, download_attempt=download_attempt)
            try:
                with self._opener.open(req, timeout=timeout) as resp:
                    if download_attempt:
                        from acquisition_gateway import read_selected_pdf
                        data = read_selected_pdf(resp)
                    else:
                        data = resp.read()
                self.governor.record_success(url)
                return data
            except urllib.error.HTTPError as exc:
                last_exc = exc
                guarded = self.governor.record_http_error(
                    exc.geturl() or url, exc.code, exc.headers.get("Retry-After") if exc.headers else None,
                    authenticated=authenticated)
                if guarded is not None and guarded.status == "RETRY_WAIT" and attempt < retries - 1:
                    continue
                if guarded is not None:
                    raise guarded from exc
                raise
            except urllib.error.URLError as exc:
                last_exc = exc
                if attempt < retries - 1:
                    time.sleep(RETRY_BACKOFF * (2 ** attempt))
                    continue
                raise
        raise RuntimeError("HTTP_ATTEMPT_BUDGET_EXHAUSTED") from last_exc

    def _get_text(self, url, timeout=30):
        return self._get_bytes(url, timeout=timeout).decode("utf-8", errors="replace")

    def _get_json(self, url, timeout=30):
        return json.loads(self._get_text(url, timeout=timeout))

    def _ncbi_url(self, endpoint, params):
        params = dict(params)
        params["tool"] = self.tool
        if self.email:
            params["email"] = self.email
        if self.api_key:
            params["api_key"] = self.api_key
        return f"{NCBI_BASE}/{endpoint}?{urllib.parse.urlencode(params)}"

    def _ncbi_get(self, endpoint, params, timeout=30):
        # NCBI 限速
        wait = NCBI_INTERVAL - (time.monotonic() - self._last_ncbi)
        if wait > 0:
            time.sleep(wait)
        self._last_ncbi = time.monotonic()
        return self._get_json(self._ncbi_url(endpoint, params), timeout=timeout)

    def _pmc_cloud_pdf_url(self, pmcid):
        """Find an OA PDF in PMC's official public AWS dataset."""
        if not pmcid or not pmcid.startswith("PMC") or not pmcid[3:].isdigit():
            return None
        prefix = f"{pmcid}."
        listing_url = f"{PMC_AWS_BUCKET}/?{urllib.parse.urlencode({'list-type': '2', 'prefix': prefix})}"
        root = ET.fromstring(self._get_bytes(listing_url))
        ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
        for contents in root.findall("s3:Contents", ns):
            key = contents.findtext("s3:Key", default="", namespaces=ns)
            filename = key.rsplit("/", 1)[-1]
            folder = key.split("/", 1)[0]
            if filename == f"{folder}.pdf" and folder.startswith(prefix):
                return f"{PMC_AWS_BUCKET}/{urllib.parse.quote(key, safe='/')}"
        return None

    # ---------- arXiv ----------
    def _arxiv_get(self, url, timeout=60):
        # arXiv 官方限速：每 3 秒最多 1 次 API 请求
        wait = ARXIV_INTERVAL - (time.monotonic() - self._last_arxiv)
        if wait > 0:
            time.sleep(wait)
        self._last_arxiv = time.monotonic()
        return self._get_text(url, timeout=timeout)

    def search_arxiv(self, query, max_results=10, sort="date"):
        params = urllib.parse.urlencode({
            "search_query": f"all:{query}",
            "start": 0,
            "max_results": max_results,
            "sortBy": "submittedDate" if sort == "date" else "relevance",
            "sortOrder": "descending",
        })
        try:
            data = self._arxiv_get(f"{ARXIV_API}?{params}", timeout=60)
        except urllib.error.HTTPError as exc:
            if exc.code == 406:
                raise RuntimeError(
                    "arXiv 返回 406（请求被限流），请降低频率后重试"
                    "（已内置每 %.0f 秒 1 次的限速）" % ARXIV_INTERVAL
                ) from exc
            raise RuntimeError(f"arXiv 请求失败：HTTP {exc.code}") from exc
        return self._parse_arxiv_feed(data)

    def resolve_arxiv_id(self, arxiv_id):
        """Refresh one exact article via the official id_list interface."""
        if not isinstance(arxiv_id, str) or not re.fullmatch(r"[A-Za-z0-9./-]{1,80}", arxiv_id):
            raise ValueError("Invalid saved arXiv ID")
        url = f"{ARXIV_API}?{urllib.parse.urlencode({'id_list': arxiv_id, 'max_results': 1})}"
        papers = self._parse_arxiv_feed(self._arxiv_get(url, timeout=60))
        return next((paper for paper in papers if paper["id"].lower() == arxiv_id.lower()), None)

    @staticmethod
    def _parse_arxiv_feed(data):
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(data)
        papers = []
        for entry in root.findall("atom:entry", ns):
            title = entry.findtext("atom:title", default="", namespaces=ns)
            summary = entry.findtext("atom:summary", default="", namespaces=ns)
            published = entry.findtext("atom:published", default="", namespaces=ns)
            authors = [a.findtext("atom:name", default="", namespaces=ns)
                       for a in entry.findall("atom:author", ns)]
            pdf_url = None
            for link in entry.findall("atom:link", ns):
                if link.get("title") == "pdf":
                    pdf_url = link.get("href")
                    break
            entry_id = entry.findtext("atom:id", default="", namespaces=ns)
            # 形如 http://arxiv.org/abs/1706.03762v2
            arxiv_id = entry_id.rstrip("/").split("/abs/")[-1]
            if not pdf_url:
                pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
            papers.append({
                "source": "arxiv",
                "id": arxiv_id,
                "title": _clean(title),
                "authors": [a for a in authors if a],
                "summary": _clean(summary),
                "published": published,
                "url": entry_id,
                "pdf_url": pdf_url,
            })
        return papers

    # ---------- PubMed ----------
    def search_pubmed(self, query, max_results=10, sort="date", open_access_only=False):
        # When selecting OA papers, inspect a recent candidate window rather than
        # returning a paywalled newest result and missing OA papers just below it.
        candidate_limit = min(max_results * 100, 100) if open_access_only else max_results
        res = self._ncbi_get("esearch.fcgi", {
            "db": "pubmed",
            "term": query,
            "retmax": candidate_limit,
            "retmode": "json",
            "sort": "pub date" if sort == "date" else "relevance",
        })
        ids = res.get("esearchresult", {}).get("idlist", [])
        if not ids:
            return []
        return self._pubmed_from_ids(ids, max_results, open_access_only)

    def resolve_pubmed_id(self, pmid):
        """Refresh one PMID directly, without searching an old keyword window."""
        if not isinstance(pmid, str) or not pmid.isascii() or not pmid.isdigit():
            raise ValueError("Invalid saved PMID")
        papers = self._pubmed_from_ids([pmid], 1, False)
        return next((paper for paper in papers if paper["id"] == pmid), None)

    def _pubmed_from_ids(self, ids, max_results, open_access_only):
        summary = self._ncbi_get("esummary.fcgi", {
            "db": "pubmed",
            "id": ",".join(ids),
            "retmode": "json",
        })
        result = summary.get("result", {})
        papers = []
        for pmid in ids:
            info = result.get(pmid, {})
            if not isinstance(info, dict) or not info.get("title"):
                continue
            pmcid = None
            doi = None
            for artid in info.get("articleids", []):
                if artid.get("idtype") == "pmc":
                    pmcid = artid.get("value")
                elif artid.get("idtype") == "doi":
                    doi = artid.get("value")
            authors = [a.get("name", "") for a in info.get("authors", [])]
            pmc_article_url = (
                f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
                if isinstance(pmcid, str) and re.fullmatch(r"PMC[0-9]{1,16}", pmcid, re.IGNORECASE)
                else None
            )
            papers.append({
                "source": "pubmed",
                "id": pmid,
                "pmcid": pmcid,
                "pmc_article_url": pmc_article_url,
                "doi": doi,
                "title": _clean(info.get("title", "")),
                "authors": [a for a in authors if a],
                "summary": "",
                "published": info.get("pubdate", ""),
                "journal": info.get("fulljournalname", ""),
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                "pdf_url": None,
            })
        # Choose one route before downloading: the verified Europe PMC PDF API
        # for matching OA/PDF core records, otherwise an explicit OA PDF link.
        # Denied requests never cause endpoint/source fallback.
        if papers:
            try:
                ids_query = " OR ".join(p["id"] for p in papers)
                url = f"{EUROPE_PMC_API}/search?{urllib.parse.urlencode({'query': f'EXT_ID:({ids_query}) AND SRC:MED', 'format': 'json', 'resultType': 'core', 'pageSize': len(papers)})}"
                results = self._get_json(url).get("resultList", {}).get("result", [])
                by_pmid = {}
                for item in results:
                    if isinstance(item, dict) and item.get('pmid'):
                        by_pmid.setdefault(str(item['pmid']), []).append(item)
                for paper in papers:
                    matches = by_pmid.get(paper['id'], [])
                    if not matches:
                        continue
                    if len(matches) != 1:
                        paper['pdf_resolution_error'] = 'CORE_IDENTITY_AMBIGUOUS'
                        continue
                    item = matches[0]
                    if (item.get('source') not in (None, 'MED')
                            or (paper.get('doi') and item.get('doi')
                                and normalized_doi(paper['doi']) != normalized_doi(item['doi']))
                            or (paper.get('pmcid') and item.get('pmcid')
                                and str(paper['pmcid']).upper() != str(item['pmcid']).upper())):
                        paper['pdf_resolution_error'] = 'CORE_IDENTITY_CONFLICT'
                        continue
                    # Abstracts are public metadata, not full-text evidence.
                    # Preserve them from the existing core response without
                    # adding an efetch request or deriving scientific claims.
                    abstract = _abstract_text(item.get("abstractText"))
                    if abstract:
                        paper["summary"] = abstract
                        paper["summary_source"] = "Europe PMC core abstractText"
                    if not paper.get("doi") and item.get("doi"):
                        paper["doi"] = item["doi"]
                    if isinstance(item.get('license'), str) and item['license'].strip():
                        paper['supplements_oa_eligible'] = item.get('isOpenAccess') == 'Y'
                        paper['supplements_license'] = item['license']
                    api_pdf = europepmc_pdf_api_url(item)
                    if api_pdf:
                        paper["pdf_url"] = api_pdf
                        paper["pmcid"] = item["pmcid"]
                    links = item.get("fullTextUrlList", {}).get("fullTextUrl", [])
                    if isinstance(links, dict):
                        links = [links]
                    for link in links:
                        if (not paper["pdf_url"] and link.get("documentStyle", "").lower() == "pdf"
                                and link.get("availabilityCode", "").upper() == "OA"
                                and link.get("url", "").startswith("https://")):
                            paper["pdf_url"] = link["url"]
                            break
                    paper["open_access"] = item.get("isOpenAccess") == "Y"
                    if (isinstance(paper.get("pmcid"), str)
                            and re.fullmatch(r"PMC[0-9]{1,16}", paper["pmcid"], re.IGNORECASE)):
                        paper["pmc_article_url"] = (
                            f"https://pmc.ncbi.nlm.nih.gov/articles/{paper['pmcid']}/")
            except GuardError:
                raise
            except Exception as exc:  # Europe PMC is an enhancement; metadata remains useful.
                if open_access_only:
                    raise RuntimeError("Europe PMC OA metadata is unavailable") from exc
                for paper in papers:
                    paper["pdf_resolution_error"] = _public_error(exc)
        if open_access_only:
            selected = []
            for paper in papers:
                if not paper.get("open_access"):
                    continue
                if not paper.get("pdf_url") and paper.get("pmcid"):
                    self._resolve_pmc_cloud_pdf(paper)
                if paper.get("pdf_url"):
                    selected.append(paper)
                    if len(selected) >= max_results:
                        break
            return selected
        selected = papers[:max_results]
        for paper in selected:
            if paper.get("open_access") and not paper.get("pdf_url") and paper.get("pmcid"):
                self._resolve_pmc_cloud_pdf(paper)
        return selected

    def _resolve_pmc_cloud_pdf(self, paper):
        try:
            paper["pdf_url"] = self._pmc_cloud_pdf_url(paper["pmcid"])
        except GuardError:
            raise
        except Exception as exc:
            paper["pdf_resolution_error"] = _public_error(exc)

    # ---------- 汇总 ----------
    def search(self, query, source="all", max_results=10, sort="date", open_access_only=False):
        """返回 (papers, errors)。单个源失败不影响其它源。"""
        papers = []
        errors = []
        if source in ("arxiv", "all"):
            try:
                papers += self.search_arxiv(query, max_results, sort=sort)
            except Exception as exc:  # noqa: BLE001
                errors.append({"source": "arxiv", "error": _public_error(exc)})
        if source in ("pubmed", "all"):
            try:
                papers += self.search_pubmed(
                    query, max_results, sort=sort, open_access_only=open_access_only
                )
            except Exception as exc:  # noqa: BLE001
                errors.append({"source": "pubmed", "error": _public_error(exc)})
        return papers, errors

    # ---------- 下载 ----------
    def download_pdf(self, paper, outdir=DEFAULT_PAPER_DIRECTORY, overwrite=False, *, authenticated=False):
        from acquisition_gateway import dispatch_selected
        return dispatch_selected("oa", "main_pdf", lambda: self._download_pdf_selected(
            paper, outdir, overwrite, authenticated=authenticated))

    def _download_pdf_selected(self, paper, outdir=DEFAULT_PAPER_DIRECTORY, overwrite=False, *, authenticated=False):
        """下载单篇论文 PDF，返回更新后的 paper 字典。"""
        if type(authenticated) is not bool:
            raise ValueError("authenticated must be a boolean")
        # Reused caller dictionaries must not carry an earlier response status.
        paper.pop('download_diagnostics', None)
        expected_md5 = paper.get("expected_pdf_md5")
        if "expected_pdf_md5" in paper and (type(expected_md5) is not str
                or re.fullmatch(r"[0-9a-fA-F]{32}", expected_md5) is None):
            paper["status"] = "INVALID_PDF"
            paper["error"] = "INVALID_SOURCE_CHECKSUM"
            return paper
        expected_md5 = expected_md5.lower() if expected_md5 is not None else None
        outdir = Path(outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        try:
            ledger = DownloadLedger(outdir)
            known = ledger.lookup(paper)
        except LedgerConflict as exc:
            paper["status"] = "NEEDS_REVIEW"
            paper["error"] = _public_error(exc)
            return paper
        if known:
            if expected_md5:
                with Path(known["pdf_path"]).open("rb") as stream:
                    digest = hashlib.md5()
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                if digest.hexdigest() != expected_md5:
                    paper["status"] = "NEEDS_REVIEW"
                    paper["error"] = "EXISTING_PDF_SOURCE_CHECKSUM_MISMATCH"
                    return paper
            paper.update(known)
            paper["status"] = "exists"
            # Ordinary PDF resume does not repeat network acquisition. A bundle
            # supplement retry has its own explicit CLI and independent receipt.
            return paper
        pdf_url = paper.get("pdf_url")
        if not pdf_url:
            paper["status"] = "skipped"
            paper["error"] = "no open-access PDF URL"
            return paper
        parsed_url = urllib.parse.urlsplit(pdf_url)
        if parsed_url.scheme == "http" and parsed_url.hostname in {"arxiv.org", "export.arxiv.org"}:
            pdf_url = urllib.parse.urlunsplit(parsed_url._replace(scheme="https"))
        elif parsed_url.scheme != "https":
            paper["status"] = "ACCESS_DENIED"
            paper["error"] = "PDF URL must use HTTPS"
            return paper
        safe = _safe_filename(paper.get("title") or paper.get("id") or "paper")
        legacy = outdir / f"{safe}.pdf"
        try:
            folder = ensure_bundle(outdir, paper, create=False)
        except (BundleError, OSError) as exc:
            paper.update(status='NEEDS_REVIEW', error=_public_error(exc))
            return paper
        path = legacy if legacy.exists() else folder / f"{safe[:80].rstrip(' .')}.pdf"
        if path.exists():
            try:
                verify_pdf(path)
            except LedgerConflict:
                paper["status"] = "INVALID_PDF"
                paper["error"] = "Existing PDF path failed size/header validation; not overwriting"
            else:
                paper["status"] = "NEEDS_REVIEW"
                paper["error"] = "Same filename exists but publication identity is unverified; not overwriting"
            if overwrite:
                paper["error"] += " (--overwrite cannot replace original PDFs)"
            return paper
        try:
            data = self._get_bytes(pdf_url, headers={"Accept": "application/pdf"}, timeout=120,
                                   download_attempt=True, authenticated=authenticated)
            if data.startswith(b"%PDF-") and len(data) >= MIN_PDF_BYTES:
                if expected_md5 and hashlib.md5(data).hexdigest() != expected_md5:
                    paper["status"] = "INVALID_PDF"
                    paper["error"] = "PDF_SOURCE_CHECKSUM_MISMATCH"
                    return paper
                from acquisition_gateway import validate_selected_pdf
                validate_selected_pdf(paper, data)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as destination:
                    destination.write(data)
                    destination.flush()
                    os.fsync(destination.fileno())
                try:
                    paper.update(ledger.record(paper, path))
                    paper["status"] = "downloaded"
                    paper.update(finish_download(outdir, paper, path, governor=self.governor))
                except LedgerConflict as exc:
                    paper["pdf_path"] = str(path)
                    paper["status"] = "NEEDS_REVIEW"
                    paper["error"] = f"PDF saved but ledger registration needs review: {_public_error(exc)}"
            else:
                if _looks_like_login_page(data):
                    status = "AUTH_EXPIRED" if authenticated else "ACCESS_DENIED"
                    paused = self.governor.pause_domain(pdf_url, status)
                    paper["status"] = paused.status
                    paper["error"] = "publisher returned a login page; domain paused for manual review"
                    paper["failure_domain"] = paused.domain
                    paper['download_diagnostics'] = {'failure_kind': 'login_html'}
                else:
                    paper["status"] = "INVALID_PDF"
                    paper["error"] = "response is not a valid PDF header or is under 1 KiB"
        except FileExistsError:
            paper["status"] = "NEEDS_REVIEW"
            paper["error"] = "PDF path appeared during download; original was not overwritten"
        except GuardError as exc:
            paper["status"] = exc.status
            paper["error"] = str(exc)
            paper["retry_at"] = exc.retry_at
            paper["failure_domain"] = exc.domain
            diagnostics = safe_download_diagnostics(exc.diagnostics)
            if not diagnostics and exc.status in {'ACCESS_DENIED', 'AUTH_EXPIRED', 'DOMAIN_PAUSED'}:
                diagnostics = {'failure_kind': 'domain_paused'}
            if diagnostics:
                paper['download_diagnostics'] = diagnostics
        except urllib.error.HTTPError as exc:
            paper['status'] = 'failed'
            paper['error'] = _public_error(exc)
            paper['download_diagnostics'] = safe_download_diagnostics(
                {'http_status': exc.code, 'failure_kind': 'http_error'})
        except Exception as exc:  # noqa: BLE001
            paper["status"] = "failed"
            paper["error"] = _public_error(exc)
        return paper


# ---------- 工具函数 ----------
def _abstract_text(value):
    """Bounded plain text from an optional public HTML abstract, no I/O."""
    if type(value) is not str or not value or len(value) > 262144 or '\x00' in value:
        return ""

    class AbstractParser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []
            self.hidden = []

        def handle_starttag(self, tag, attrs):
            if tag in {'script', 'style'}:
                self.hidden.append(tag)
            elif tag in {'p', 'div', 'br', 'h1', 'h2', 'h3', 'h4', 'li'} and not self.hidden:
                self.parts.append(' ')

        def handle_endtag(self, tag):
            if self.hidden:
                if tag == self.hidden[-1]:
                    self.hidden.pop()
            elif tag in {'p', 'div', 'h1', 'h2', 'h3', 'h4', 'li'}:
                self.parts.append(' ')

        def handle_data(self, data):
            if not self.hidden:
                self.parts.append(data)

    try:
        parser = AbstractParser()
        parser.feed(value)
        parser.close()
        text = _clean(''.join(parser.parts))
        if len(text) > 65536 or any(unicodedata.category(char) == 'Cc' for char in text):
            return ""
        return text
    except (ValueError, TypeError, RecursionError):
        return ""


def _clean(text):
    return " ".join((text or "").replace("\n", " ").split())


def _looks_like_login_page(data: bytes) -> bool:
    """Recognize explicit HTML authentication pages without logging their body."""
    sample = data[:32768].decode("utf-8", errors="ignore").lower()
    if not re.search(r"<\s*(?:!doctype\s+html|html\b|form\b)", sample):
        return False
    explicit_form = bool(re.search(
        r"<input\b[^>]*\btype\s*=\s*['\"]?password\b|"
        r"<input\b[^>]*\bname\s*=\s*['\"]?(?:username|user|j_username)\b", sample))
    auth_markers = (
        "shibboleth", "openathens", "samlrequest", "institutional login",
        "sign in to your institution", "access through your institution",
        "authentication required", "login required", "please log in",
        "please sign in", "session has expired", "session expired",
    )
    return explicit_form or any(marker in sample for marker in auth_markers)


def _safe_filename(name):
    name = unicodedata.normalize("NFC", name)
    name = "".join("_" if c in '<>:"/\\|?*' or ord(c) < 32 else c for c in name)
    name = " ".join(name.split()).strip(" .")
    name = name[:160].rstrip(" .")
    if not name or name.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        return "paper"
    return name


def _to_json(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _public_error(exc):
    """Return a useful failure class without serializing URLs or credential values."""
    if isinstance(exc, GuardError):
        return str(exc)  # status, hostname and retry time only
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return f"Network error ({type(exc.reason).__name__})"
    if isinstance(exc, LedgerConflict):
        return "PDF or publication identifiers need manual review"
    if isinstance(exc, DownloadInUse):
        return "Another downloader process is active"
    if isinstance(exc, PlanError):
        return str(exc)  # validator messages contain no untrusted plan content
    return type(exc).__name__


def _zotero_blocked_result(command, exc):
    code = getattr(exc, "code", None)
    if code not in {"ZOTERO_UNAVAILABLE", "ZOTERO_API_ERROR"}:
        code = "ZOTERO_API_ERROR"
    status = "unavailable" if code == "ZOTERO_UNAVAILABLE" else "error"
    preflight = {"status": status, "reason_code": code, "duplicate": None,
                 "safe_to_continue": False, "writes_performed": False}
    return {"command": command, "status": "blocked", "count": 0, "papers": [],
            "zotero_preflight": preflight,
            "errors": [{"source": "zotero", "error": code}]}


def _apply_zotero_preflight(paper, items):
    try:
        result = compare_with_zotero(paper, items)
    except InvalidCandidateError:
        result = {"status": "error", "reason_code": "INVALID_PAPER_METADATA",
                  "duplicate": None, "safe_to_continue": False,
                  "checked_items": len(items), "matches": [], "writes_performed": False}
    except Exception:
        result = {"status": "error", "reason_code": "ZOTERO_API_ERROR",
                  "duplicate": None, "safe_to_continue": False,
                  "checked_items": len(items), "matches": [], "writes_performed": False}
    paper["zotero_preflight"] = result
    if result["safe_to_continue"]:
        return True
    status_by_result = {
        "duplicate": "ZOTERO_DUPLICATE",
        "title_match": "ZOTERO_TITLE_REVIEW",
        "conflict": "ZOTERO_CONFLICT",
    }
    paper["status"] = status_by_result.get(result["status"], "ZOTERO_PREFLIGHT_INVALID")
    paper["error"] = result["reason_code"]
    return False


def _run_zotero_preflight(args):
    paper = {"source": "pubmed", "id": args.pmid, "doi": args.doi,
             "pmcid": args.pmcid, "title": args.title}
    try:
        result = preflight_paper(paper, client=ZoteroLocalApi())
    except InvalidCandidateError:
        result = {"status": "invalid_input", "reason_code": "INVALID_PAPER_METADATA",
                  "duplicate": None, "safe_to_continue": False,
                  "writes_performed": False}
        exit_code = 2
    except Exception as exc:  # noqa: BLE001 - raw local API errors are never public
        code = getattr(exc, "code", None)
        if code not in {"ZOTERO_UNAVAILABLE", "ZOTERO_API_ERROR"}:
            code = "ZOTERO_API_ERROR"
        result = {"status": "unavailable" if code == "ZOTERO_UNAVAILABLE" else "error",
                  "reason_code": code, "duplicate": None, "safe_to_continue": False,
                  "writes_performed": False}
        exit_code = 2
    else:
        exit_code = 0 if result["safe_to_continue"] else 3

    payload = {"command": "zotero-preflight", **result}
    if args.json:
        print(_to_json(payload))
    elif result.get("status") == "clear":
        print(f"未找到 Zotero 重复项（检查 {result['checked_items']} 条记录）；仅完成只读预检。")
    elif result.get("matches"):
        print(f"Zotero 预检结果：{result['status']}；发现 {len(result['matches'])} 个匹配/待核对条目，未执行下载或写入。")
    else:
        print(f"Zotero 预检未通过：{result['reason_code']}；未执行下载或写入。", file=sys.stderr)
    return exit_code


def _public_paper(paper):
    """Whitelist CLI fields; keep publisher PDF URLs and raw diagnostics private."""
    fields = ("source", "id", "pmcid", "pmc_article_url", "doi", "title", "authors", "summary", "summary_source", "published",
              "journal", "open_access", "status", "pdf_path", "size_bytes", "sha256",
              "matched_identifiers", "retry_at", "failure_domain", "zotero_preflight",
              "bundle_path", "paper_directory", "supplements_status", "supplements_error")
    result = {field: paper[field] for field in fields if field in paper}
    if isinstance(paper.get('supplements'), list):
        public_fields = ('status', 'filename', 'relative_path', 'content_type', 'size_bytes',
                         'sha256', 'source', 'license', 'error')
        result['supplements'] = [{k: row[k] for k in public_fields if k in row}
                                 for row in paper['supplements'] if isinstance(row, dict)]
    diagnostics = safe_download_diagnostics(paper.get('download_diagnostics'))
    if diagnostics:
        result['download_diagnostics'] = diagnostics
    result["url"] = sanitized_source_page(paper.get("url"))
    result["pdf_available"] = bool(paper.get("pdf_url"))
    if paper.get("error"):
        # Status and retry/domain fields are the stable public failure contract.
        result["error_code"] = str(paper.get("status") or "FAILED")
    if paper.get("pdf_resolution_error"):
        result["pdf_resolution_error_code"] = "PDF_RESOLUTION_FAILED"
    return result


def _paper_from_job(job):
    """Build only a local identity stub; PDF URLs are always refreshed by ID."""
    source = job.get("source")
    source_id = str(job.get("source_id") or "")
    if source not in {"pubmed", "arxiv", "doi"} or not source_id:
        raise ValueError("Saved job has no supported source ID")
    paper = {"source": source, "id": source_id, "title": job.get("title") or source_id,
             "authors": [], "pdf_url": None}
    for identifier in job.get("identifiers", []):
        if isinstance(identifier, str) and identifier.startswith("doi:"):
            doi = normalized_doi(identifier[4:])
            if doi:
                paper["doi"] = doi
        elif isinstance(identifier, str) and identifier.startswith("pmcid:"):
            paper["pmcid"] = identifier[6:]
    if source == "pubmed":
        if not source_id.isascii() or not source_id.isdigit():
            raise ValueError("Saved PMID is invalid")
        paper["url"] = f"https://pubmed.ncbi.nlm.nih.gov/{source_id}/"
    elif source == 'arxiv':
        if not re.fullmatch(r"[A-Za-z0-9./-]{1,80}", source_id):
            raise ValueError("Saved arXiv ID is invalid")
        paper["url"] = f"https://arxiv.org/abs/{source_id}"
    else:
        doi = normalized_doi(source_id)
        if not doi or normalized_doi(paper.get('doi')) != doi:
            raise ValueError('Saved DOI job is invalid')
        paper['id'] = doi
        paper['url'] = 'https://doi.org/' + doi
    return paper


def _identity_changed(before, after):
    for field in ("doi", "pmcid"):
        old = before.get(field)
        new = after.get(field)
        if field == "doi":
            old, new = normalized_doi(old), normalized_doi(new)
        if old and new and old != new:
            return True
    return False


def _print_papers(papers):
    for i, p in enumerate(papers, 1):
        print(f"[{i}] ({p['source']}) {p['title']}")
        print(f"    authors: {', '.join(p['authors'][:5])}{'...' if len(p['authors']) > 5 else ''}")
        print(f"    id: {p['id']}  PDF available: {'yes' if p.get('pdf_url') else 'no'}")
        if p.get("pdf_path"):
            print(f"    saved: {p['pdf_path']}")


# ---------- CLI ----------
def build_parser():
    parser = argparse.ArgumentParser(
        prog="paper_downloader",
        description="根据关键字自动搜索并下载论文（arXiv / PubMed），供 agent 自动调用。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--query", "-q", required=True, help="搜索关键字，如 'large language model'")
    common.add_argument("--source", "-s", choices=["arxiv", "pubmed", "all"], default="all",
                        help="数据源，默认 all")
    common.add_argument("--max", "-m", type=int, default=10, help="每个源最大结果数，默认 10")
    common.add_argument("--email", default=None, help="NCBI 联系邮箱（提升配额）")
    common.add_argument("--tool", default="paper_downloader", help="NCBI tool 标识")
    common.add_argument("--sort", choices=["date", "relevance"], default="date",
                        help="PubMed 排序方式，默认按最新发表时间")
    common.add_argument("--open-access-only", action="store_true",
                        help="仅返回有 Europe PMC OA PDF 链接的 PubMed 文献；按最新发表时间筛选")
    common.add_argument("--state-path", default=str(Path(__file__).with_name("download_state.json")),
                        help="域名限速/冷却状态文件；不包含 cookie、URL 或凭据")

    p_search = sub.add_parser("search", parents=[common], help="仅搜索，不下载")
    p_search.add_argument("--json", action="store_true", help="以 JSON 输出结果")

    p_download = sub.add_parser("download", parents=[common], help="搜索并下载 PDF")
    p_download.add_argument("--outdir", "-o", default=str(DEFAULT_PAPER_DIRECTORY), help="Paper bundle output root; uses configured machine default")
    p_download.add_argument("--overwrite", action="store_true", help="兼容旧参数；为保护原始 PDF，现不会覆盖已存在文件")
    p_download.add_argument("--zotero-preflight", action="store_true",
                            help="下载前只读检查 Zotero；未运行、命中或歧义时阻止获取 PDF")
    p_download.add_argument("--json", action="store_true", help="以 JSON 输出结果")

    p_zotero = sub.add_parser("zotero-preflight", help="只读检查 Zotero 中是否已有同一论文")
    p_zotero.add_argument("--doi", help="论文 DOI")
    p_zotero.add_argument("--pmid", help="PubMed PMID")
    p_zotero.add_argument("--pmcid", help="PubMed Central PMCID")
    p_zotero.add_argument("--title", help="论文题名；仅当规范化后完全相同时匹配")
    p_zotero.add_argument("--json", action="store_true", help="以 JSON 输出结果")

    p_resume = sub.add_parser("resume", help="按已保存的论文 ID 恢复未完成下载，不重跑旧检索词")
    p_resume.add_argument("--outdir", "-o", type=Path, required=True, help="已有下载任务目录")
    p_resume.add_argument("--max", "-m", type=int, default=10, help="本次最多处理的待恢复论文数")
    p_resume.add_argument("--state-path", default=str(Path(__file__).with_name("download_state.json")))
    p_resume.add_argument("--email", default=None)
    p_resume.add_argument("--tool", default="paper_downloader")
    p_resume.add_argument("--zotero-preflight", action="store_true",
                           help="恢复下载前只读检查 Zotero；未运行、命中或歧义时阻止获取")
    p_resume.add_argument("--json", action="store_true")

    p_batch = sub.add_parser("batch-plan", help="校验并按安全的 JSON 检索计划逐项下载")
    p_batch.add_argument("--plan", type=Path, required=True, help="只包含版本与检索任务的本地 JSON 文件")
    p_batch.add_argument("--outdir", "-o", type=Path, default=DEFAULT_PAPER_DIRECTORY, help="Paper bundle output root; uses configured machine default")
    p_batch.add_argument("--execute", action="store_true", help="实际搜索与下载；默认仅校验计划，不联网")
    p_batch.add_argument("--zotero-preflight", action="store_true",
                         help="执行前只读检查 Zotero；未运行、命中或歧义时阻止检索/获取")
    p_batch.add_argument("--state-path", default=str(Path(__file__).with_name("download_state.json")))
    p_batch.add_argument("--email", default=None)
    p_batch.add_argument("--tool", default="paper_downloader")
    p_batch.add_argument("--json", action="store_true")

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if hasattr(args, "max") and args.max < 1:
        parser.error("--max 必须大于 0")
    if args.command == "batch-plan" and args.zotero_preflight and not args.execute:
        parser.error("batch-plan --zotero-preflight 需要同时指定 --execute")
    try:
        # A fixed lock for this installation prevents alternate --state-path
        # values from running parallel requests around the per-domain limit.
        with process_lock(Path(__file__).with_name("paper_downloader-global")):
            return _run(args)
    except (DownloadInUse, LedgerConflict, OSError, ValueError) as exc:
        error = {"source": "downloader", "error": f"BUSY_OR_UNAVAILABLE: {_public_error(exc)}"}
        if args.json:
            print(_to_json({"command": args.command, "query": getattr(args, "query", None),
                            "source": getattr(args, "source", None), "count": 0,
                            "open_access_only": getattr(args, "open_access_only", False),
                            "papers": [], "errors": [error]}))
        else:
            print(error["error"], file=sys.stderr)
        return 2


def _run(args):
    if args.command == "zotero-preflight":
        return _run_zotero_preflight(args)
    if args.command == "resume":
        return _run_resume(args)
    if args.command == "batch-plan":
        return _run_batch_plan(args)

    zotero_items = None
    if args.command == "download" and args.zotero_preflight:
        try:
            # Check local Zotero before any public search request. If Zotero is
            # closed or its API is incomplete, this invocation fails closed.
            zotero_items = ZoteroLocalApi().scan_items()
        except Exception as exc:  # noqa: BLE001 - fail closed with sanitized output
            result = _zotero_blocked_result("download", exc)
            if args.json:
                print(_to_json(result))
            else:
                print("Zotero 只读预检不可用；未开始检索或下载。", file=sys.stderr)
            return 2

    dl = PaperDownloader(email=args.email, tool=args.tool, api_key=os.environ.get("NCBI_API_KEY"),
                         state_path=args.state_path)
    papers, errors = dl.search(
        args.query, source=args.source, max_results=args.max, sort=args.sort,
        open_access_only=args.open_access_only,
    )

    if args.command == "download":
        outdir = Path(args.outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        jobs = DownloadJobStore(outdir)
        for p in papers:
            if zotero_items is not None and not _apply_zotero_preflight(p, zotero_items):
                continue
            key = jobs.ensure(p, args.query)
            if not jobs.prepare(key, p, DownloadLedger(outdir)):
                continue
            jobs.mark_fetching(key)
            dl.download_pdf(p, outdir, overwrite=args.overwrite)
            jobs.record_result(key, p, DownloadLedger(outdir))

    failed_downloads = [p for p in papers if p.get("status") not in ("downloaded", "exists")] if args.command == "download" else []

    if args.json:
        print(_to_json({
            "query": args.query,
            "source": args.source,
            "count": len(papers),
            "open_access_only": args.open_access_only,
            "papers": [_public_paper(p) for p in papers],
            "errors": errors,
        }))
    else:
        for e in errors:
            print(f"[warn] {e['source']} 搜索失败：{e['error']}", file=sys.stderr)
        if args.command == "download":
            ok = sum(1 for p in papers if p.get("status") in ("downloaded", "exists"))
            print(f"共 {len(papers)} 篇，成功获取 {ok} 篇 PDF，失败 {len(failed_downloads)} 篇。")
        _print_papers(papers)

    # 退出码：1=所有数据源均失败；2=部分失败
    if errors and not papers:
        return 1
    if errors or failed_downloads:
        return 2
    return 0


def _run_batch_plan(args):
    tasks = load_plan(args.plan)
    if not args.execute:
        result = {"command": "batch-plan", "mode": "dry-run", "tasks": tasks,
                  "max_candidates": sum(task["max_results"] for task in tasks),
                  "network_requests": 0}
        if args.json:
            print(_to_json(result))
        else:
            print(f"计划预检通过：{len(tasks)} 个检索任务；上限 {result['max_candidates']} 篇；未联网。")
        return 0

    zotero_items = None
    if args.zotero_preflight:
        try:
            # Fail before creating an output directory or contacting search APIs.
            zotero_items = ZoteroLocalApi().scan_items()
        except Exception as exc:  # noqa: BLE001 - fail closed with sanitized output
            result = _zotero_blocked_result("batch-plan", exc)
            result.update({"mode": "execute", "tasks": [], "count": 0,
                           "errors": 1, "incomplete": True})
            if args.json:
                print(_to_json(result))
            else:
                print("Zotero 只读预检不可用；未开始检索或下载。", file=sys.stderr)
            return 2

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    jobs = DownloadJobStore(outdir)
    dl = PaperDownloader(email=args.email, tool=args.tool, api_key=os.environ.get("NCBI_API_KEY"),
                         state_path=args.state_path)
    summaries = []
    total_papers = 0
    total_errors = 0
    for index, task in enumerate(tasks, 1):
        try:
            papers, errors = dl.search(task["query"], source=task["source"],
                                       max_results=task["max_results"], sort=task["sort"],
                                       open_access_only=task["open_access_only"])
        except Exception as exc:  # one search task must not abort the other tasks
            papers, errors = [], [{"source": task["source"], "error": _public_error(exc)}]
        papers = papers[:task["max_results"]]
        for paper in papers:
            if zotero_items is not None and not _apply_zotero_preflight(paper, zotero_items):
                continue
            try:
                key = jobs.ensure(paper, task["query"])
            except ValueError:
                paper["status"] = "NEEDS_REVIEW"
                paper["error"] = "Publication identity needs manual review"
                continue
            if not jobs.prepare(key, paper, DownloadLedger(outdir)):
                continue
            jobs.mark_fetching(key)
            dl.download_pdf(paper, outdir)
            jobs.record_result(key, paper, DownloadLedger(outdir))
        total_papers += len(papers)
        total_errors += len(errors)
        summaries.append({"task": index, "source": task["source"], "count": len(papers),
                          "papers": [_public_paper(paper) for paper in papers], "errors": errors})
    incomplete = any(not summary["papers"] or any(paper.get("status") not in ("downloaded", "exists")
                                                    for paper in summary["papers"]) for summary in summaries)
    result = {"command": "batch-plan", "mode": "execute", "tasks": summaries,
              "count": total_papers, "errors": total_errors, "incomplete": incomplete}
    if args.json:
        print(_to_json(result))
    else:
        saved = sum(paper.get("status") in ("downloaded", "exists")
                    for summary in summaries for paper in summary["papers"])
        print(f"处理 {len(tasks)} 个检索任务、{total_papers} 篇候选；已获取或验证 {saved} 篇。")
    if total_errors and total_papers == 0:
        return 1
    return 2 if total_errors or incomplete else 0


def _run_resume(args):
    outdir = Path(args.outdir)
    if not (outdir / ".paper-download-jobs.json").is_file():
        raise ValueError("No saved download jobs in the requested directory")
    zotero_items = None
    if args.zotero_preflight:
        try:
            # Verify Zotero before refreshing IDs or requesting any PDF URLs.
            zotero_items = ZoteroLocalApi().scan_items()
        except Exception as exc:  # noqa: BLE001 - fail closed with sanitized output
            result = _zotero_blocked_result("resume", exc)
            if args.json:
                print(_to_json(result))
            else:
                print("Zotero 只读预检不可用；未恢复下载。", file=sys.stderr)
            return 2
    jobs = DownloadJobStore(outdir)
    dl = PaperDownloader(email=args.email, tool=args.tool, api_key=os.environ.get("NCBI_API_KEY"),
                         state_path=args.state_path)
    verified_saved = 0
    for key, job in list(jobs.state["jobs"].items()):
        if isinstance(job, dict) and job.get("status") == "SAVED":
            saved = _paper_from_job(job)
            if f"{saved['source']}:{saved['id'].lower()}" != key:
                raise ValueError("Saved job key and source ID disagree")
            jobs.prepare(key, saved, DownloadLedger(outdir))  # hash check, no network
            verified_saved += saved.get("status") == "exists"
    papers = []
    for key in jobs.resumable_keys(args.max):
        job = jobs.state["jobs"][key]
        stub = _paper_from_job(job)
        if f"{stub['source']}:{stub['id'].lower()}" != key:
            raise ValueError("Saved job key and source ID disagree")
        if zotero_items is not None and not _apply_zotero_preflight(stub, zotero_items):
            papers.append(stub)
            continue
        if not jobs.prepare(key, stub, DownloadLedger(outdir)):
            papers.append(stub)
            continue
        if stub['source'] == 'doi':
            stub.update(status='NEEDS_REVIEW', error='DOI acquisition requires explicit route selection')
            jobs.record_result(key, stub, DownloadLedger(outdir))
            papers.append(stub)
            continue
        jobs.mark_fetching(key)
        try:
            fresh = (dl.resolve_pubmed_id(stub["id"]) if stub["source"] == "pubmed"
                     else dl.resolve_arxiv_id(stub["id"]))
            if fresh is None:
                stub.update(status="FAILED", error="Saved source ID no longer resolves")
                paper = stub
            elif fresh.get("source") != stub["source"] or fresh.get("id") != stub["id"]:
                stub.update(status="NEEDS_REVIEW", error="Resolved paper has a different source ID")
                paper = stub
            elif _identity_changed(stub, fresh):
                stub.update(status="NEEDS_REVIEW", error="Publication identifiers changed")
                paper = stub
            elif fresh.get("pdf_resolution_error") and not fresh.get("pdf_url"):
                paper = stub
                paper.update(status="RETRY_WAIT", error="OA PDF resolution is temporarily unavailable",
                             retry_at=time.time() + 300)
            else:
                paper = fresh
                jobs.ensure(paper, job.get("query") or "")
                dl.download_pdf(paper, outdir)
        except GuardError as exc:
            paper = stub
            paper.update(status=exc.status, error=_public_error(exc),
                         retry_at=exc.retry_at, failure_domain=exc.domain)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            paper = stub
            paper.update(status="RETRY_WAIT", error=_public_error(exc),
                         retry_at=time.time() + 300)
        except Exception as exc:  # unexpected metadata/schema error; manual review
            paper = stub
            paper.update(status="FAILED", error=_public_error(exc))
        jobs.record_result(key, paper, DownloadLedger(outdir))
        papers.append(paper)
    paused = {}
    unfinished = 0
    for job in jobs.state["jobs"].values():
        if job.get("status") != "SAVED":
            unfinished += 1
        if job.get("status") in {"AUTH_REQUIRED", "AUTH_EXPIRED", "ACCESS_DENIED",
                                 "DOMAIN_PAUSED", "INVALID_PDF", "FAILED", "NEEDS_REVIEW"}:
            status = job["status"]
            paused[status] = paused.get(status, 0) + 1
    result = {"command": "resume", "count": len(papers), "papers": [_public_paper(p) for p in papers],
              "verified_saved": verified_saved,
              "paused": paused, "due_remaining": len(jobs.resumable_keys(10**9)),
              "unfinished": unfinished, "errors": []}
    if args.json:
        print(_to_json(result))
    else:
        print(f"恢复检查 {len(papers)} 篇；仍未完成 {unfinished} 篇；人工暂停 {sum(paused.values())} 篇。")
        _print_papers(papers)
    return 2 if unfinished else 0


if __name__ == "__main__":
    sys.exit(main())
