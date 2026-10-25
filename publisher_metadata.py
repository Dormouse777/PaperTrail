"""Explicit public metadata requests; preview never networks or reads keys."""
import json
import re
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode
from pathlib import Path

from download_guard import GuardError
from process_lock import process_lock
from pmc_open_data_metadata import validate_pmc_metadata


class MetadataError(ValueError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise MetadataError('METADATA_REDIRECT_REQUIRES_REVIEW')


class PublisherMetadataClient:
    """GET once per source, no automatic failover after transport refusal.

    Caller constructs a governor with <=10 requests/min and >=5s spacing.
    No real client is constructed or called by the CLI's default preview.
    The injected budget callback must durably reserve each metadata attempt
    before I/O. Missing callback refuses online access (100/day not inferred
    from RequestGovernor's unrelated hourly PDF allowance).
    """
    def __init__(self, governor, *, email=None, transport=None,
                 budget_reserver=None, noncommercial_research=False):
        if type(noncommercial_research) is not bool:
            raise MetadataError('NONCOMMERCIAL_FLAG_INVALID')
        if email is not None and (type(email) is not str or
                not re.fullmatch(r'[^\s@&?#]+@[^\s@&?#]+\.[^\s@&?#]+', email)):
            raise MetadataError('CONTACT_EMAIL_INVALID')
        self.governor, self.email, self.transport = governor, email, transport
        self.budget_reserver = budget_reserver
        self.noncommercial_research = noncommercial_research

    def _get(self, url):
        if not callable(self.budget_reserver):
            raise MetadataError('DAILY_REQUEST_BUDGET_REQUIRED')
        if self.governor.interval < 5 or self.governor.per_minute > 10:
            raise MetadataError('METADATA_RATE_POLICY_INVALID')
        with process_lock(Path(__file__).with_name('paper_downloader-global')):
            self.budget_reserver(url)
            self.governor.acquire(url, download_attempt=False)
            try:
                if self.transport is not None:
                    status, headers, body = self.transport(url)
                else:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
                    request = urllib.request.Request(url, headers={
                        'Accept': 'application/json', 'User-Agent': 'TraeResearchMetadata/1.0'})
                    try:
                        with opener.open(request, timeout=20) as response:
                            status, headers = response.status, response.headers
                            body = response.read(2 * 1024 * 1024 + 1)
                    except urllib.error.HTTPError as error:
                        status, headers, body = error.code, error.headers, b''
                if status == 404:
                    return None
                if status != 200:
                    guard_status = 500 if 500 <= status <= 599 and status not in {500, 502, 503, 504} else status
                    failure = self.governor.record_http_error(url, guard_status, headers.get('Retry-After'))
                    raise MetadataError(failure.status if failure else 'METADATA_HTTP_STOPPED')
                if type(body) is not bytes or len(body) > 2 * 1024 * 1024:
                    raise MetadataError('METADATA_RESPONSE_INVALID')
                value = json.loads(body.decode('utf-8'))
                if type(value) is not dict:
                    raise MetadataError('METADATA_RESPONSE_INVALID')
                self.governor.record_success(url)
                return value
            except (MetadataError, GuardError):
                raise
            except Exception:
                raise MetadataError('METADATA_TRANSPORT_OR_FORMAT_FAILED') from None

    def get_crossref(self, doi, *, preview):
        if preview is True:
            return None
        from publisher_router import normalize_doi
        doi = normalize_doi(doi)
        return self._get('https://api.crossref.org/works/' + quote(doi, safe=''))

    def get_openalex(self, doi, *, preview):
        """Public discovery only; OA status is not a download permission gate."""
        if preview is True:
            return None
        from publisher_router import normalize_doi
        doi = normalize_doi(doi)
        return self._get('https://api.openalex.org/works/doi:' + quote(doi, safe=''))

    def get_europepmc(self, doi, *, preview):
        """Search precise DOI without a key/email; keep N distinct from Y."""
        if preview is True:
            return None
        from publisher_router import normalize_doi
        doi = normalize_doi(doi)
        return self._get('https://www.ebi.ac.uk/europepmc/webservices/rest/search?' +
            urlencode({'query': 'DOI:"' + doi + '"', 'format': 'json',
                       'pageSize': 5, 'resultType': 'core'}))

    def get_unpaywall(self, doi, *, preview):
        if preview is True or self.email is None:
            return None
        from publisher_router import normalize_doi
        doi = normalize_doi(doi)
        # No credentials; public API identifies its maintainer by contact email.
        return self._get('https://api.unpaywall.org/v2/' + quote(doi, safe='') +
                         '?' + urlencode({'email': self.email}))

    def get_pmc(self, doi, *, preview):
        if preview is True or self.email is None:
            return None
        from publisher_router import normalize_doi
        doi = normalize_doi(doi)
        identifiers = self._get('https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/?' +
            urlencode({'ids': doi, 'idtype': 'doi', 'format': 'json',
                       'tool': 'TraeResearchRouting', 'email': self.email}))
        if identifiers is None:
            return None
        records = identifiers.get('records')
        if identifiers.get('status') != 'ok' or type(records) is not list or len(records) != 1:
            raise MetadataError('PMC_IDENTITY_RESPONSE_INVALID')
        record = records[0]
        if type(record) is not dict:
            raise MetadataError('PMC_IDENTITY_RESPONSE_INVALID')
        if record.get('status') == 'error' or record.get('errmsg') or record.get('live') is False:
            return None
        try:
            if normalize_doi(record.get('doi')) != doi:
                raise ValueError()
        except ValueError:
            raise MetadataError('PMC_DOI_MISMATCH') from None
        pmcid, pmid = record.get('pmcid'), str(record.get('pmid', ''))
        if type(pmcid) is not str or not re.fullmatch(r'PMC[1-9][0-9]*', pmcid) or not pmid.isdecimal():
            raise MetadataError('PMC_IDENTITY_RESPONSE_INVALID')
        # Deliberately original v1, not an unverified assertion of "latest".
        metadata = self._get('https://pmc-oa-opendata.s3.amazonaws.com/metadata/' + pmcid + '.1.json')
        if metadata is None:
            return None
        validated = validate_pmc_metadata(metadata, expected_pmid=pmid,
            expected_pmcid=pmcid, expected_doi=doi, expected_title=metadata.get('title'),
            noncommercial_research=self.noncommercial_research)
        return {**validated, 'pdf_url': validated['anonymous_pdf_url']}
