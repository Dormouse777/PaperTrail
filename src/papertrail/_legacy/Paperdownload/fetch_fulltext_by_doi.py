"""Multi-index full-text discovery and explicitly scoped PDF download.

No OA-only filter. Default preview performs no network, secrets or writes.
"""
import argparse
import json
from pathlib import Path

from publisher_router import normalize_doi
from fetch_by_doi import _load
from workflow_defaults import default_path

ROOT = Path(__file__).resolve().parent
PROVIDERS = ('openalex', 'europepmc', 'crossref')


def discover(doi, client, providers=PROVIDERS, *, response_sink=None):
    from fulltext_candidates import parse_candidates
    rows = []
    for provider in providers:
        if provider not in PROVIDERS:
            raise ValueError('FULLTEXT_PROVIDER_INVALID')
        response = getattr(client, 'get_' + provider)(doi, preview=False)
        if response is not None:
            rows.extend(parse_candidates(provider, response, doi))
            if response_sink is not None:
                response_sink[provider] = response
    return rows


def prepare_queue_handoff(download_dir, outcome, paper, *, confirmed, permission_evidence):
    """Publish public bibliography only after the existing SAVED/P5 checks.

    This is NOT a Zotero/parse/index execution or external-data authorization.
    Caller binds paper to freshly requested public metadata before PDF fetch.
    """
    from publishers.base import _clean_public_reference
    if confirmed is not True or _clean_public_reference(permission_evidence) is None:
        raise ValueError('FULLTEXT_LOCAL_PROCESSING_RIGHTS_REQUIRED')
    if (outcome.get('source') not in {'fulltext:openalex', 'fulltext:europepmc', 'fulltext:crossref'}
            or outcome.get('license') != 'user-confirmed-access-not-oa'):
        raise ValueError('FULLTEXT_QUEUE_SOURCE_MISMATCH')
    from publisher_filing_bridge import bridge_publisher_result
    checked = bridge_publisher_result(download_dir, outcome, paper,
                                     license_confirmed=True, execute=False)
    if checked.get('status') != 'READY':
        return {'status': 'NEEDS_REVIEW', 'reason_code': checked.get('reason_code'),
                'binding_verified': False, 'downstream_executed': False}
    from download_jobs import DownloadJobStore
    from zotero_filing import bibliographic_data, FilingError
    try:
        bibliographic_data(paper, paper)
    except FilingError:
        return {'status': 'NEEDS_REVIEW', 'reason_code': 'FULLTEXT_QUEUE_BIBLIOGRAPHY_INCOMPLETE',
                'binding_verified': False, 'downstream_executed': False}
    # Preserve the actual public authors/date/journal in the standard SAVED
    # bibliography. Never create an alternate queue or overwrite PDF/status.
    try:
        key = DownloadJobStore(download_dir).ensure(paper, 'fulltext controlled handoff')
    except (OSError, ValueError, TypeError, RuntimeError):
        # A verified PDF already exists. Do not describe it as an unsuccessful
        # download or blindly start another fetch when queue enrichment fails.
        return {'status': 'NEEDS_REVIEW', 'reason_code': 'FULLTEXT_QUEUE_STATE_UNAVAILABLE',
                'binding_verified': False, 'downstream_executed': False}
    checked = bridge_publisher_result(download_dir, outcome, paper,
                                     license_confirmed=True, execute=False)
    if checked.get('status') != 'READY' or checked.get('job_key') != key:
        return {'status': 'NEEDS_REVIEW', 'reason_code': 'FULLTEXT_QUEUE_BINDING_CHANGED',
                'binding_verified': False, 'downstream_executed': False}
    return {'status': 'READY', 'reason_code': None, 'job_key': key,
            'binding_verified': True, 'downstream_executed': False,
            'local_processing_permission_evidence': permission_evidence,
            'external_upload_authorized': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--doi', required=True)
    parser.add_argument('--providers', default=','.join(PROVIDERS))
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--discover', action='store_true')
    modes.add_argument('--execute', action='store_true')
    parser.add_argument('--metadata-file', type=Path, help='Offline responses keyed by provider; never authorize live download')
    parser.add_argument('--candidate-id', help='Exact SHA256 selection from a prior discovery')
    parser.add_argument('--confirm-access-rights', action='store_true')
    parser.add_argument('--permission-evidence', help='Public HTTPS reference for lawful retrieval; not an OA assertion')
    parser.add_argument('--noncommercial-research', action='store_true')
    parser.add_argument('--download-dir', type=Path, default=default_path('download_dir', 'data/papers'))
    parser.add_argument('--prepare-queue', action='store_true',
                        help='Explicit single PubMed SAVED handoff; does not archive/parse/index')
    parser.add_argument('--pmid', help='Exact PubMed ID, independently checked in fresh Europe PMC core')
    parser.add_argument('--confirm-local-processing-rights', action='store_true')
    parser.add_argument('--local-processing-permission-evidence',
                        help='Separate public reference for local archive/processing, not external upload')
    args = parser.parse_args(argv)
    try:
        doi = normalize_doi(args.doi)
        providers = tuple(args.providers.split(','))
        if not providers or len(set(providers)) != len(providers) or any(p not in PROVIDERS for p in providers):
            raise ValueError('FULLTEXT_PROVIDER_INVALID')
        from fulltext_download import candidate_id, validate_selection
        from publishers.base import _clean_public_reference
        import re
        handoff_fields = (args.pmid, args.confirm_local_processing_rights,
                          args.local_processing_permission_evidence)
        if args.prepare_queue:
            if (args.pmid is None or re.fullmatch(r'[0-9]{1,16}', args.pmid) is None
                    or 'europepmc' not in providers or not args.confirm_local_processing_rights
                    or _clean_public_reference(args.local_processing_permission_evidence) is None
                    or args.discover or args.metadata_file):
                raise ValueError('FULLTEXT_QUEUE_EXPLICIT_SCOPE_REQUIRED')
        elif any(handoff_fields):
            raise ValueError('FULLTEXT_QUEUE_FLAG_REQUIRED')
        if args.execute:
            if (args.metadata_file or not args.confirm_access_rights or not args.noncommercial_research
                    or not args.candidate_id or not re.fullmatch('[0-9a-f]{64}', args.candidate_id)
                    or _clean_public_reference(args.permission_evidence) is None):
                raise ValueError('FULLTEXT_EXPLICIT_SCOPE_REQUIRED')
        if not args.discover and not args.execute:
            candidates = []
            if args.metadata_file:
                from fulltext_candidates import parse_candidates
                response, _ = _load(args.metadata_file)
                for provider in providers:
                    if provider in response:
                        candidates.extend(parse_candidates(provider, response[provider], doi))
            result = {'mode': 'offline-preview', 'doi': doi, 'providers': providers,
                      'candidates': candidates, 'credential_reads': 0, 'network_requests': 0,
                      'writes': False, 'oa_only': False, 'downloaded': False}
            if args.prepare_queue:
                result['queue_handoff'] = {'status': 'preview', 'pmid': args.pmid,
                    'binding_verified': False, 'downstream_executed': False,
                    'external_upload_authorized': False}
        else:
            if args.metadata_file:
                raise ValueError('OFFLINE_CACHE_CANNOT_AUTHORIZE_LIVE_FETCH')
            from download_guard import RequestGovernor
            from publisher_metadata import PublisherMetadataClient
            from process_lock import process_lock
            from publishers.base import GLOBAL_PROCESS_LOCK, reserve_daily_request
            with process_lock(GLOBAL_PROCESS_LOCK):
                governor = RequestGovernor(ROOT / 'download_state.json', interval_seconds=10,
                                           max_requests_per_minute=6)
                client = PublisherMetadataClient(governor,
                    budget_reserver=lambda url: reserve_daily_request(args.download_dir, url, 100),
                    noncommercial_research=args.noncommercial_research)
                responses = {} if args.prepare_queue else None
                candidates = (discover(doi, client, providers, response_sink=responses)
                              if args.prepare_queue else discover(doi, client, providers))
                result = {'mode': 'fulltext-discovery', 'doi': doi, 'candidates': candidates,
                          'oa_only': False, 'credential_reads': 0, 'downloaded': False}
                if args.execute:
                    matches = [c for c in candidates if candidate_id(c) == args.candidate_id]
                    if len(matches) != 1:
                        raise ValueError('FULLTEXT_CANDIDATE_CHANGED_OR_ABSENT')
                    selected = validate_selection(matches[0], doi=doi, confirmed=True,
                                                  permission_evidence=args.permission_evidence)
                    paper = None
                    if args.prepare_queue:
                        from pubmed_fulltext_binding import pubmed_paper_from_core
                        paper = pubmed_paper_from_core(responses.get('europepmc'), doi=doi,
                                                     pmid=args.pmid, title=selected['title'])
                    from fulltext_download import PublicFulltextAdapter, make_download_plan
                    adapter = PublicFulltextAdapter(selected, confirmed=True,
                                                    permission_evidence=args.permission_evidence)
                    fetch_options = {'paper': paper} if args.prepare_queue else {}
                    outcome = adapter.fetch_pdf(make_download_plan(selected), args.download_dir, governor,
                                                noncommercial_research=True, **fetch_options)
                    result.update(mode='fulltext-download-and-queue' if args.prepare_queue else 'fulltext-download-only', download=outcome,
                                  downloaded=outcome['status'] in ('downloaded', 'exists'))
                    if args.prepare_queue and result['downloaded']:
                        result['queue_handoff'] = prepare_queue_handoff(args.download_dir, outcome, paper,
                            confirmed=True, permission_evidence=args.local_processing_permission_evidence)
        from fulltext_download import candidate_id
        result['candidates'] = [dict(c, candidate_id=candidate_id(c)) for c in result['candidates']]
        print(json.dumps(result, ensure_ascii=True, indent=2))
        passed = result['downloaded'] and (not args.prepare_queue or
                                          result.get('queue_handoff', {}).get('binding_verified') is True)
        return 0 if not args.execute or passed else 2
    except Exception:
        print(json.dumps({'status': 'stopped', 'reason': 'FULLTEXT_INPUT_SOURCE_OR_ACCESS_REVIEW_REQUIRED',
                          'credential_reads': 0, 'downloaded': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
