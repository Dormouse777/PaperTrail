"""Publisher routing CLI: default offline preview, explicit bounded execution."""
import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path

from publisher_router import resolve_dois, normalize_doi
from workflow_defaults import default_path

ROOT = Path(__file__).resolve().parent
REGISTRY = ROOT / 'publisher_registry.json'


class CachedMetadata:
    def __init__(self, rows):
        if type(rows) is not dict:
            raise ValueError('METADATA_CACHE_INVALID')
        self.rows = rows
    def get_crossref(self, doi, *, preview):
        return self.rows.get(doi, {}).get('crossref')
    def get_unpaywall(self, doi, *, preview):
        return self.rows.get(doi, {}).get('unpaywall')
    def get_pmc(self, doi, *, preview):
        return self.rows.get(doi, {}).get('pmc')


def _load(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('ROUTING_INPUT_FILE_INVALID')
    raw = path.read_bytes()
    # Ambiguous approval/cache documents must never silently use the last key.
    # Reject non-standard numeric constants as well; reviewed bytes and parsed
    # policy must describe the same, ordinary JSON document.
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('ROUTING_INPUT_DUPLICATE_KEY')
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError('ROUTING_INPUT_NONSTANDARD_JSON')

    value = json.loads(raw, object_pairs_hook=unique_object,
                       parse_constant=reject_constant)
    if type(value) is not dict:
        raise ValueError('ROUTING_INPUT_FILE_INVALID')
    return value, hashlib.sha256(raw).hexdigest()


def build_preview(dois, registry, *, cache=None, noncommercial_research=False):
    plans = resolve_dois(dois, registry, preview=True,
        # CachedMetadata cannot perform I/O. Opt in to *parsing that cache*,
        # while an ordinary preview still does not resolve any metadata.
        resolve_metadata=cache is not None,
        metadata_client=CachedMetadata(cache) if cache is not None else None,
        noncommercial_research=noncommercial_research)
    return dict(mode='offline-preview', plans=[asdict(p) for p in plans],
        manual_queue=[{'doi': p.doi, 'reason': p.reason} for p in plans if p.action == 'manual'],
        network_requests=0, credential_reads=0, pdf_attempts=0, writes=False,
        live_acceptance=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--dois')
    source.add_argument('--dois-file', type=Path)
    parser.add_argument('--registry', type=Path, default=REGISTRY)
    parser.add_argument('--metadata-file', type=Path, help='Public offline fixtures keyed by normalized DOI')
    parser.add_argument('--download-dir', type=Path, default=default_path('download_dir', 'data/papers'))
    parser.add_argument('--expected-title', help='Caller title for local duplicate preflight, not proof of identity')
    parser.add_argument('--resource-type', choices=('pdf', 'jats'), default='pdf',
                        help='Springer JATS is a separate XML resource, never a PDF')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--preview', action='store_true')
    mode.add_argument('--resolve-metadata', action='store_true', help='Explicit public metadata only, no PDF/key')
    mode.add_argument('--execute', action='store_true')
    parser.add_argument('--contact-email')
    parser.add_argument('--publisher-channel', choices=('elsevier', 'wiley', 'springer_nature'),
                        help='Restrict this bounded test to one subscription channel; OA stays first')
    parser.add_argument('--noncommercial-research', action='store_true')
    parser.add_argument('--confirm-public-download', action='store_true')
    parser.add_argument('--confirm-publisher-download', action='store_true')
    parser.add_argument('--download-only', action='store_true', help='Explicit standalone PDF/ledger only; no Zotero filing')
    parser.add_argument('--elsevier-insttoken', action='store_true',
                        help='Explicitly use approved ELSEVIER_INSTTOKEN with this Elsevier key; never auto-discover')
    parser.add_argument('--wiley-api-redirect', action='store_true',
                        help='Authorize one official Wiley API-directed anonymous PDF hop for this single DOI')
    args = parser.parse_args(argv)
    try:
        if args.wiley_api_redirect and (args.publisher_channel != 'wiley'
                or args.resource_type != 'pdf' or args.resolve_metadata
                or (args.execute and not args.confirm_publisher_download)):
            raise ValueError('WILEY_SINGLE_ARTICLE_SCOPE_INVALID')
        if args.elsevier_insttoken and (args.publisher_channel != 'elsevier'
                or args.resource_type != 'pdf'
                or (args.execute and not args.confirm_publisher_download)):
            raise ValueError('ELSEVIER_AUTH_MODE_INVALID')
        if args.dois_file:
            if args.dois_file.is_symlink() or args.dois_file.stat().st_size > 16384:
                raise ValueError('DOI_INPUT_FILE_INVALID')
            raw_dois = args.dois_file.read_text(encoding='utf-8').splitlines()
        else:
            raw_dois = args.dois.split(',')
        dois = list(dict.fromkeys(normalize_doi(d.strip()) for d in raw_dois if d.strip()))
        if not 1 <= len(dois) <= 20:
            raise ValueError('DOI_BATCH_OUT_OF_SCOPE')
        # Refuse before any guard files, credentials, metadata or PDF request.
        if args.execute and (len(dois) != 1 or not args.confirm_public_download
                             or not args.download_only or not args.noncommercial_research):
            raise ValueError('EXPLICIT_SINGLE_PAPER_DOWNLOAD_ONLY_CONFIRMATION_REQUIRED')
        if args.metadata_file and (args.execute or args.resolve_metadata):
            raise ValueError('OFFLINE_CACHE_CANNOT_AUTHORIZE_LIVE_FETCH')
        if args.expected_title is not None:
            if len(dois) != 1 or args.resource_type != 'pdf':
                raise ValueError('EXISTING_TITLE_SINGLE_PDF_REQUIRED')
            from existing_pdf_preflight import check_existing_title_pdf
            local_check = check_existing_title_pdf(args.download_dir, doi=dois[0], title=args.expected_title)
            if local_check['status'] == 'review_required':
                print(json.dumps({'mode': 'existing-pdf-preflight', **local_check,
                    'network_requests': 0, 'credential_reads': 0, 'writes': False,
                    'content_identity_validated': False, 'live_acceptance': False}))
                return 2
        registry, registry_sha = _load(args.registry)
        selected_credential = None
        if args.publisher_channel:
            from publisher_setup_check import validate_test_selection
            if len(dois) != 1:
                raise ValueError('PUBLISHER_TEST_SINGLE_DOI_REQUIRED')
            selection = validate_test_selection(registry, args.publisher_channel, dois[0])
            selected_credential = selection['credential_variable']
            if (args.resource_type == 'jats') != (args.publisher_channel == 'springer_nature'):
                raise ValueError('PUBLISHER_TEST_RESOURCE_MISMATCH')
        if args.resource_type == 'jats':
            if len(dois) != 1 or args.metadata_file or args.resolve_metadata:
                raise ValueError('JATS_SINGLE_DOI_WITHOUT_METADATA_MODE_REQUIRED')
            if args.execute and not args.confirm_publisher_download:
                raise ValueError('JATS_PUBLISHER_CONFIRMATION_REQUIRED')
            from publishers import create_adapter
            current, current_sha = _load(args.registry)
            if current_sha != registry_sha:
                raise ValueError('REGISTRY_CHANGED_REVIEW_REQUIRED')
            entry = current.get('springer_nature')
            if type(entry) is not dict:
                raise ValueError('CURRENT_ADAPTER_REGISTRY_INVALID')
            adapter = create_adapter('springer_nature', entry)
            outcome = adapter.fetch_jats(dois[0], args.download_dir,
                execute=args.execute, confirmed=args.confirm_publisher_download,
                noncommercial_research=args.noncommercial_research)
            print(json.dumps(dict(mode='jats-download-only' if args.execute else 'jats-preview',
                resource=outcome, zotero_filed=False, downstream_processed=False,
                live_acceptance=False), ensure_ascii=False, indent=2))
            return (0 if not args.execute or outcome['status'] in ('downloaded_xml', 'exists_xml') else 2)
        if not args.execute and not args.resolve_metadata:
            cache = _load(args.metadata_file)[0] if args.metadata_file else None
            result = build_preview(dois, registry, cache=cache,
                                   noncommercial_research=args.noncommercial_research)
        else:
            from download_guard import RequestGovernor
            from publisher_metadata import PublisherMetadataClient
            from process_lock import process_lock
            from publishers.base import GLOBAL_PROCESS_LOCK, reserve_daily_request
            publisher_confirmation = args.execute and args.confirm_publisher_download
            # Metadata HTTP attempts and their shared daily reservations must use
            # the same installation-wide lock as publisher adapters. Keep setup
            # inside the lock too, so a conflict exits before any metadata I/O.
            with process_lock(GLOBAL_PROCESS_LOCK):
                # A single conservative governor covers every configured
                # publisher, including Wiley's stricter 60/10min TDM limit.
                governor = RequestGovernor(ROOT / 'download_state.json', interval_seconds=10,
                                           max_requests_per_minute=6)
                def reserve(url):
                    return reserve_daily_request(args.download_dir, url, limit=100)
                client = PublisherMetadataClient(governor, email=args.contact_email,
                    budget_reserver=reserve, noncommercial_research=args.noncommercial_research)
                plans = resolve_dois(dois, registry, preview=False, metadata_client=client,
                    credential_present=(lambda name: (selected_credential is None or name == selected_credential)
                                        and bool(os.environ.get(name))) if publisher_confirmation else None,
                    noncommercial_research=args.noncommercial_research,
                    resolve_metadata=True)
            result = dict(mode='download-only' if args.execute else 'metadata-only',
                plans=[asdict(p) for p in plans],
                manual_queue=[{'doi': p.doi, 'reason': p.reason} for p in plans if p.action == 'manual'],
                downloads=[], zotero_filed=False, downstream_processed=False, live_acceptance=False)
            if args.execute:
                from publishers import create_adapter
                current, current_sha = _load(args.registry)
                if current_sha != registry_sha:
                    raise ValueError('REGISTRY_CHANGED_REVIEW_REQUIRED')
                plan = plans[0]
                if plan.action == 'download':
                    if args.publisher_channel and plan.adapter not in ('oa', args.publisher_channel):
                        raise ValueError('PUBLISHER_TEST_ROUTE_CHANGED')
                    if plan.adapter != 'oa' and not args.confirm_publisher_download:
                        raise ValueError('PUBLISHER_STAGE_CONFIRMATION_REQUIRED')
                    entry = current.get('oa' if plan.adapter == 'oa' else plan.publisher)
                    if type(entry) is not dict:
                        raise ValueError('CURRENT_ADAPTER_REGISTRY_INVALID')
                    # Caller supplies a fresh registry snapshot and no credential values.
                    if args.elsevier_insttoken and plan.adapter == 'elsevier':
                        adapter = create_adapter(plan.adapter, entry, elsevier_insttoken=True)
                    elif args.wiley_api_redirect and plan.adapter == 'wiley':
                        adapter = create_adapter(plan.adapter, entry, wiley_redirect_doi=dois[0])
                    else:
                        adapter = create_adapter(plan.adapter, entry)
                    fetch_options = {'noncommercial_research': args.noncommercial_research}
                    if args.expected_title is not None:
                        fetch_options['expected_title'] = args.expected_title
                    outcome = adapter.fetch_pdf(plan, args.download_dir, governor, **fetch_options)
                    result['downloads'].append(outcome)
        if args.wiley_api_redirect:
            result['wiley_redirect_scope'] = {'doi': dois[0], 'max_api_requests': 1,
                'max_anonymous_hops': 1, 'credential_forwarding': False,
                'long_term_allowlist_changed': False}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.execute:
            return 0 if result['downloads'] and all(r['status'] in ('downloaded', 'exists')
                                                    for r in result['downloads']) else 2
        return 0
    except Exception:
        # Input/library exceptions can contain paths, URLs or credential text.
        print(json.dumps({'status': 'stopped', 'reason_code': 'ROUTING_INPUT_OR_STAGE_REVIEW_REQUIRED'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
