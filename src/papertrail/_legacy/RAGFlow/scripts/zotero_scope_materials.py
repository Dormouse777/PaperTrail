"""Selected Zotero material acquisition/preparation using existing adapters."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import quote, urlsplit, urljoin
import xml.etree.ElementTree as ET

from zotero_scope_workflow import read, scope_digest, validate_authorization
from zotero_scope_online import AuditBudget, AuditStop, atomic_json
from download_guard import RequestGovernor, GuardError
from publishers.base import UrllibTransport
from paper_supplements import _valid_license


class OfficialSources:
    def __init__(self, scope, authorization, output):
        validate_authorization(scope, authorization)
        self.scope, self.output = scope, Path(output)
        self.budget = AuditBudget(scope, authorization, self.output / "online-budget.json")
        self.governor = RequestGovernor(self.output / "public-request-state.json", interval_seconds=10,
            max_requests_per_minute=6, max_auto_wait_seconds=60, jitter_seconds=0)
        self.transport = UrllibTransport()
        self.allowed_hosts = set()
        for destination in authorization["destinations"]:
            if destination["service"] == "公开官方论文及补件来源":
                self.allowed_hosts.update(urlsplit("https://" + x).hostname for x in destination["destination"])

    def get(self, url, *, tag, limit=8 * 1024 * 1024):
        if urlsplit(url).scheme != "https" or urlsplit(url).hostname not in self.allowed_hosts:
            raise AuditStop("OFFICIAL_SOURCE_OUT_OF_BATCH")
        path = self.output / "official" / (tag + ".json")
        if path.exists():
            cached = read(path)
            if cached["url"] != url or cached["scope_sha256"] != scope_digest(self.scope):
                raise AuditStop("OFFICIAL_SOURCE_CHECKPOINT_DIFFERS")
            return cached
        self.governor.acquire(url)
        event = self.budget.reserve("official:" + url, {"public_metadata_or_page_requests": 1})
        result = {"url": url, "scope_sha256": scope_digest(self.scope),
                  "checked_at": datetime.now(timezone.utc).isoformat(), "status": None}
        response = None
        try:
            response = self.transport.get(url, headers={"Accept": "application/json, application/xml, text/html"},
                                          timeout=45, allow_redirects=False)
            result["status"] = response.status
            raw = response.read(limit + 1)
            if len(raw) > limit:
                raise ValueError("OFFICIAL_RESPONSE_SIZE_LIMIT")
            result.update(content_type=response.headers.get("Content-Type", ""),
                          body_sha256=hashlib.sha256(raw).hexdigest(), body=raw.decode("utf-8", errors="replace"))
            if response.status == 200:
                self.governor.record_success(url)
            else:
                self.governor.record_http_error(url, response.status, response.headers.get("Retry-After"))
            self.budget.finish(event, response={"status": response.status, "body_sha256": result["body_sha256"]})
        except (OSError, ValueError, GuardError) as error:
            result["error"] = str(error)
            self.budget.finish(event, failed=True)
        finally:
            if response is not None:
                response.close()
        atomic_json(path, result)
        return result


def discover_official_materials(scope, authorization, output):
    """Persist exact official observations. Not-found and failed stay distinct."""
    sources = OfficialSources(scope, authorization, output)
    results = []
    previous_path = Path(output) / "official-materials.json"
    previous = {r["key"]: r for r in read(previous_path)["papers"]} if previous_path.exists() else {}
    snapshot = read(Path(output) / "identity-snapshot.json")
    for paper in scope["papers"]:
        if snapshot["papers"][paper["key"]]["parent"]["data"]["DOI"].lower() != paper["doi"].lower():
            raise AuditStop("DISCOVERY_PARENT_IDENTITY_DIFFERS")
        result = {"key": paper["key"], "doi": paper["doi"], "status": "not_checked", "discovered": []}
        if previous.get(paper["key"], {}).get("publisher_check"):
            result["publisher_check"] = previous[paper["key"]]["publisher_check"]
        try:
            url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=" + quote('DOI:"' + paper["doi"] + '"') + "&format=json&resultType=core&pageSize=5"
            metadata = sources.get(url, tag=paper["key"] + "-metadata")
            result["metadata_evidence"] = str(sources.output / "official" / (paper["key"] + "-metadata.json"))
            if metadata["status"] != 200:
                result.update(status="check_failed", reason="OFFICIAL_METADATA_UNAVAILABLE")
            else:
                records = [r for r in json.loads(metadata["body"]).get("resultList", {}).get("result", [])
                           if r.get("doi", "").lower() == paper["doi"].lower()]
                if not records:
                    result.update(status="checked_not_found", reason="欧洲PMC未找到此DOI记录；不等于出版商无补件")
                else:
                    record = records[0]
                    result["metadata"] = {k: record.get(k) for k in ("id", "pmcid", "doi", "title", "isOpenAccess", "license")}
                    pmcid = record.get("pmcid")
                    if not pmcid:
                        result.update(status="checked_unavailable", reason="官方元数据有论文，无可检查的PMC全文；出版商补件仍待检查")
                    else:
                        xml_url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
                        document = sources.get(xml_url, tag=paper["key"] + "-article")
                        result["source_url"] = xml_url
                        if document["status"] != 200:
                            result.update(status="check_failed", reason="OFFICIAL_FULLTEXT_XML_UNAVAILABLE")
                        else:
                            root = ET.fromstring(document["body"])
                            identifiers = {"".join(e.itertext()).strip().lower() for e in root.findall(".//article-meta/article-id[@pub-id-type='doi']")}
                            if paper["doi"].lower() not in identifiers:
                                raise AuditStop("OFFICIAL_XML_DOI_DIFFERS")
                            files = []
                            for node in root.findall(".//supplementary-material"):
                                hrefs = [e.get("{http://www.w3.org/1999/xlink}href") for e in node.iter()]
                                for href in hrefs:
                                    if href:
                                        files.append({"href": href, "label": " ".join(node.itertext()).strip()[:500]})
                            result["discovered"] = list({f["href"]: f for f in files}.values())
                            result["xml_evidence"] = str(sources.output / "official" / (paper["key"] + "-article.json"))
                            result["license"] = record.get("license")
                            result["eligible_pmc_supplements"] = record.get("isOpenAccess") == "Y" and _valid_license(record.get("license"))
                            result.update(status="checked_found" if files else "checked_no_files_in_source",
                                checked_at=document["checked_at"], reason=None if files else "官方PMC XML未列出附件；此结论仅限所检查来源")
        except (ValueError, OSError, GuardError, ET.ParseError) as error:
            result.update(status="check_failed", reason=str(error))
        results.append(result)
        atomic_json(Path(output) / "official-materials.json", {"scope_sha256": scope_digest(scope), "papers": results})
        print("Official check " + paper["key"] + ": " + result["status"], flush=True)
    return results


def check_publisher_gaps(scope, authorization, output):
    """Check primary publisher pages without guessing file URLs or bypassing denial."""
    sources = OfficialSources(scope, authorization, output)
    path = Path(output) / "official-materials.json"
    document = read(path)
    processing = read(Path(output) / "processing.json") if (Path(output) / "processing.json").exists() else {"papers": {}}
    for row in document["papers"]:
        acquisition = processing["papers"].get(row["key"], {}).get("acquisition", {})
        failed_acquisition = acquisition.get("supplements_status") == "partial" and not any(r.get("status") in ("downloaded", "exists") for r in acquisition.get("supplements", []))
        if row.get("publisher_check") or row.get("eligible_pmc_supplements") and row.get("discovered") and not failed_acquisition:
            continue
        doi = row["doi"]
        check = {"status": "not_checked", "links": []}
        try:
            metadata = sources.get("https://api.crossref.org/works/" + quote(doi, safe=""), tag=row["key"] + "-crossref")
            candidates = []
            if metadata["status"] == 200:
                work = json.loads(metadata["body"])["message"]
                if work.get("DOI", "").lower() != doi.lower():
                    raise AuditStop("CROSSREF_DOI_DIFFERS")
                candidates.extend(work.get("resource", {}).get("primary", {}).get(k) for k in ("URL",))
                candidates.extend(link.get("URL") for link in work.get("link", []))
            if doi.startswith("10.1038/"):
                candidates.insert(0, "https://www.nature.com/articles/" + doi.split("/", 1)[1])
            if doi.startswith("10.1126/"):
                candidates.insert(0, "https://www.science.org/doi/" + doi)
            if doi.startswith("10.3390/"):
                candidates.insert(0, "https://www.mdpi.com/2073-4409/10/6/1339")
            # An exact Crossref-resolved official landing page is sufficient.
            urls = [u for u in candidates if isinstance(u, str) and urlsplit(u).scheme == "https"
                and urlsplit(u).hostname in sources.allowed_hosts and urlsplit(u).hostname != "doi.org"]
            if not urls:
                check.update(status="check_unavailable", reason="官方Crossref未给出授权目的地内可检查的出版商页面")
            else:
                response = sources.get(urls[0], tag=row["key"] + "-publisher")
                check.update(source_url=urls[0], checked_at=response["checked_at"], http_status=response["status"],
                    evidence=str(sources.output / "official" / (row["key"] + "-publisher.json")))
                if response["status"] != 200:
                    check.update(status="check_failed", reason=response.get("error") or "PUBLISHER_HTTP_" + str(response["status"]))
                else:
                    links = []
                    for href in re.findall(r'''(?:href|src)=["']([^"']+)["']''', response["body"], flags=re.I):
                        if re.search(r"suppl|moesm|mmc\d|suppdata|esm\.pdf", href, flags=re.I):
                            absolute = urljoin(urls[0], href.replace("&amp;", "&"))
                            if absolute.startswith("https://"):
                                links.append(absolute)
                    check.update(status="checked_links_found" if links else "checked_no_links_in_page", links=list(dict.fromkeys(links)))
        except (ValueError, OSError, GuardError) as error:
            check.update(status="check_failed", reason=str(error))
        row["publisher_check"] = check
        atomic_json(path, document)
        print("Publisher check " + row["key"] + ": " + check["status"], flush=True)
    return document["papers"]


def inspect_linkinghub_landings(scope, authorization, output):
    """Use an explicitly advertised official landing URL, never a guessed PII."""
    from urllib.parse import unquote
    sources = OfficialSources(scope, authorization, output)
    path = Path(output) / "official-materials.json"
    document = read(path)
    for row in document["papers"]:
        check = row.get("publisher_check", {})
        if "linkinghub.elsevier.com" not in check.get("source_url", "") or check.get("article_landing_check"):
            continue
        response = read(check["evidence"])
        if "<title>Redirecting</title>" not in response.get("body", ""):
            continue
        check.update(status="check_incomplete", reason="所取页面仅为出版商跳转页，不能判定无补件")
        urls = re.findall(r"https%3A%2F%2Fwww\.sciencedirect\.com%2Fscience%2Farticle%2Fpii%2F[A-Z0-9]+", response["body"])
        if urls:
            url = unquote(urls[0])
            try:
                landing = sources.get(url, tag=row["key"] + "-publisher-article")
            except GuardError as error:
                check["article_landing_check"] = {"url": url, "status": "domain_paused", "error": str(error)}
                atomic_json(path, document)
                continue
            check["article_landing_check"] = {"url": url, "status": landing["status"],
                "checked_at": landing["checked_at"], "error": landing.get("error"),
                "evidence": str(sources.output / "official" / (row["key"] + "-publisher-article.json"))}
            if landing["status"] == 200:
                links = re.findall(r'''href=["']([^"']*(?:suppl|mmc\d|suppdata)[^"']*)["']''', landing["body"], flags=re.I)
                check["article_landing_check"]["links"] = [urljoin(url, href) for href in links]
        atomic_json(path, document)
    return document["papers"]
