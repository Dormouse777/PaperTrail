"""Explicit rights-confirmed public PDF retrieval, including non-OA papers.

Discovery is not authority. No keys, publisher APIs, redirects or XML-as-PDF.
The existing publisher adapters remain responsible for authenticated channels.
"""
from copy import deepcopy
from io import BytesIO
from urllib.parse import urlsplit
from pdf_article_identity import ArticleIdentityError, verify_article_identity

from publishers.base import (PublisherAdapter, SourceSpec, AdapterFailure,
                             _normalize_doi, _validated_https_url, _clean_public_reference,
                             _read_response)
from fulltext_url import validated_public_fulltext_url

PROVIDERS = {'openalex', 'crossref', 'europepmc', 'publisher_oa'}
AUTH_HOSTS = {'api.elsevier.com', 'api.wiley.com', 'api.springernature.com'}


def candidate_id(candidate):
    import hashlib
    import json
    return hashlib.sha256(json.dumps(candidate, ensure_ascii=True, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('ascii')).hexdigest()


def validate_selection(candidate, *, doi, confirmed, permission_evidence):
    """Validate before constructor/transport/network; never assume OA is enough."""
    if type(candidate) is not dict or candidate.get('provider') not in PROVIDERS:
        raise ValueError('FULLTEXT_CANDIDATE_INVALID')
    if _normalize_doi(candidate.get('doi')) != _normalize_doi(doi):
        raise ValueError('FULLTEXT_DOI_MISMATCH')
    if candidate.get('format') != 'pdf':
        raise ValueError('FULLTEXT_PDF_REQUIRED_XML_IS_SEPARATE')
    title = candidate.get('title')
    if (type(title) is not str or not 1 <= len(title) <= 2048
            or any(ord(c) < 32 for c in title)):
        raise ValueError('FULLTEXT_TITLE_REQUIRED')
    url = candidate.get('url')
    parsed = urlsplit(url) if type(url) is str else None
    host = parsed.hostname.lower() if parsed and parsed.hostname else ''
    if (not host or host in AUTH_HOSTS or candidate.get('access_route') != 'direct'
            or candidate.get('intended_application') in ('similarity-checking', 'text-mining')):
        raise ValueError('FULLTEXT_AUTHORIZED_PUBLISHER_CHANNEL_REQUIRED')
    if validated_public_fulltext_url(url) is None:
        raise ValueError('FULLTEXT_PUBLIC_URL_REQUIRED')
    if confirmed is not True or _clean_public_reference(permission_evidence) is None:
        raise ValueError('FULLTEXT_ACCESS_RIGHTS_CONFIRMATION_REQUIRED')
    if candidate['provider'] == 'publisher_oa':
        # Official article evidence is distinct from a Crossref TDM link or
        # authenticated XML API. Bind this narrow OA route to the actual main
        # PDF, never an ESM/peer-review attachment or an arbitrary public host.
        from publishers.oa_adapter import _cc_license
        expected = '/content/pdf/' + _normalize_doi(doi) + '.pdf'
        evidence_url = urlsplit(permission_evidence)
        if (host not in {'link.springer.com', 'link.springernature.com'}
                or parsed.path != expected or parsed.query
                or evidence_url.scheme != 'https'
                or evidence_url.hostname != host
                or evidence_url.path != '/article/' + _normalize_doi(doi)
                or evidence_url.query or evidence_url.fragment
                or candidate.get('availability') != 'open_access'
                or _cc_license(candidate.get('license')) is None):
            raise ValueError('PUBLISHER_OA_MAIN_PDF_EVIDENCE_REQUIRED')
    return deepcopy(candidate)


class _BufferedResponse:
    def __init__(self, original, body):
        self.original = original
        self.status = getattr(original, 'status', getattr(original, 'status_code', None))
        self.headers = original.headers
        self.stream = BytesIO(body)
    def read(self, size):
        return self.stream.read(size)
    def close(self):
        self.stream.close()
        self.original.close()


class _IdentityTransport:
    def __init__(self, delegate, candidate):
        self.delegate, self.candidate = delegate, candidate
        self.consumed = False
        self.failure_code = None
    def get(self, url, *, headers, timeout, allow_redirects=False):
        if (self.consumed or url != self.candidate['url'] or allow_redirects is not False
                or headers != {'Accept': 'application/pdf'}):
            raise ValueError('FULLTEXT_SINGLE_REQUEST_SCOPE_INVALID')
        self.consumed = True
        response = self.delegate.get(url, headers=headers, timeout=timeout, allow_redirects=False)
        if getattr(response, 'status', getattr(response, 'status_code', None)) != 200:
            return response
        try:
            mime = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
            if mime != 'application/pdf':
                self.failure_code = 'FULLTEXT_NOT_PDF'
                raise ValueError('FULLTEXT_NOT_PDF')
            body = _read_response(response)
            try:
                verify_article_identity(body, doi=self.candidate['doi'], title=self.candidate['title'])
            except ArticleIdentityError as failure:
                self.failure_code = ('FULLTEXT_SUPPLEMENT_NOT_ARTICLE'
                    if failure.code == 'ARTICLE_SUPPLEMENT_NOT_MAIN_TEXT'
                    else 'FULLTEXT_PDF_BODY_INVALID' if failure.code == 'ARTICLE_PDF_UNREADABLE'
                    else 'FULLTEXT_PAPER_IDENTITY_MISMATCH')
                raise ValueError(self.failure_code) from None
            return _BufferedResponse(response, body)
        except Exception:
            if self.failure_code is None:
                self.failure_code = 'FULLTEXT_PDF_BODY_INVALID'
            response.close()
            # No raw parser/server exception or URL escapes into a result.
            raise ValueError('FULLTEXT_MIME_OR_PAPER_IDENTITY_FAILED') from None


class PublicFulltextAdapter(PublisherAdapter):
    adapter_ids = ('public_fulltext',)
    source_id = 'fulltext:public'
    def __init__(self, candidate, *, confirmed=False, permission_evidence=None, transport=None):
        self.candidate = validate_selection(candidate, doi=candidate.get('doi'),
            confirmed=confirmed, permission_evidence=permission_evidence)
        self.evidence = permission_evidence
        self._default_publisher_transport = candidate.get('provider') == 'publisher_oa' and transport is None
        host = urlsplit(self.candidate['url']).hostname.lower()
        super().__init__({'rate_limit': {'interval_seconds': 10, 'per_minute': 6, 'per_day': 100}},
                         transport=transport)
        self.host = host
        self.source_id = 'fulltext:' + self.candidate['provider']
        self.transport = _IdentityTransport(self.transport, self.candidate)
    def _allowed_hosts(self):
        return {self.host}
    def _prepare_source(self, plan, paper, doi, noncommercial_research, permission_evidence):
        if (doi != self.candidate['doi'] or plan.get('candidate_id') != candidate_id(self.candidate)
                or plan.get('title') != self.candidate['title']):
            raise AdapterFailure('manual', 'FULLTEXT_CANDIDATE_SCOPE_MISMATCH')
        if noncommercial_research is not True:
            raise AdapterFailure('manual', 'NONCOMMERCIAL_RESEARCH_REQUIRED')
        return SourceSpec(doi=doi, url=self.candidate['url'], source=self.source_id,
            channel=self.candidate['provider'], license='user-confirmed-access-not-oa',
            permission_evidence=self.evidence, allow_query=bool(urlsplit(self.candidate['url']).query),
            allowed_query=urlsplit(self.candidate['url']).query or None)
    def _request_headers(self, credential):
        return {'Accept': 'application/pdf'}
    def fetch_pdf(self, *args, **kwargs):
        if self._default_publisher_transport:
            from publisher_oa_transport import SpringerPublicPDFTransport
            outdir = args[1] if len(args) > 1 else kwargs['outdir']
            governor = args[2] if len(args) > 2 else kwargs['governor']
            self.transport.delegate = SpringerPublicPDFTransport(self.candidate['url'], governor, outdir,
                delegate=self.transport.delegate)
            self._default_publisher_transport = False
        outcome = super().fetch_pdf(*args, **kwargs)
        if outcome.get('error') == 'TRANSPORT_ERROR' and self.transport.failure_code:
            outcome.update(status='INVALID_PDF', error=self.transport.failure_code,
                           publiccode=self.transport.failure_code)
        return outcome

    def _validate_article(self, data, identity, *, existing=False):
        # The same candidate identity applies to reused files, not just new HTTP bodies.
        if identity.get('title') != self.candidate['title']:
            raise AdapterFailure('NEEDS_REVIEW', 'FULLTEXT_CANDIDATE_SCOPE_MISMATCH')
        return super()._validate_article(data, identity, existing=existing)


def make_download_plan(candidate):
    return {'doi': candidate['doi'], 'title': candidate['title'], 'adapter': 'public_fulltext',
            'action': 'download', 'candidate_id': candidate_id(candidate)}
