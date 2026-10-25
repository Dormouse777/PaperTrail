"""Acquire one paper through existing modules; execute continues Zotero, local
document parsing and main/supplement indexing. Default invocation is a read-only
preview. Dify is not called. Use capabilities for effects and stage boundaries.
"""
import argparse
import json
from pathlib import Path
import re
import sys
from workflow_defaults import default_path, local_setting

ROOT = Path(__file__).resolve().parent
INPUT_LIMIT = 16384
OBSERVATION_FIELDS = frozenset({
    'record_date', 'browser', 'official_entry_domain',
    'institution_visible_identifier', 'selected_database_public_host',
    'resource_reachability', 'reentry_observation', 'limited_user_confirmation',
})


class QuietParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes rejected input, potentially a secret URL.
        raise ValueError('ACCESS_INPUT_INVALID')


def build_observation(raw):
    """Validate caller-supplied observations, not a browser/login assertion."""
    if type(raw) is not str or len(raw.encode('utf-8')) > INPUT_LIMIT:
        raise ValueError('ACCESS_INPUT_INVALID')

    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('ACCESS_INPUT_INVALID')
            value[key] = item
        return value

    def reject_constant(value):
        raise ValueError('ACCESS_INPUT_INVALID')

    value = json.loads(raw, object_pairs_hook=unique_object,
                       parse_constant=reject_constant)
    if type(value) is not dict or set(value) != OBSERVATION_FIELDS:
        raise ValueError('ACCESS_INPUT_INVALID')
    # Import from this checkout, not an unrelated installed institution package.
    project = str(ROOT.parent)
    if project not in sys.path:
        sys.path.insert(0, project)
    from institution.acceptance_record import create_acceptance_record, serialize_acceptance_record
    record = create_acceptance_record(**value)
    return json.loads(serialize_acceptance_record(record))


def load_acquisition(path):
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError('ACCESS_INPUT_INVALID')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('ACCESS_INPUT_INVALID')
            value[key] = item
        return value
    def reject(value):
        raise ValueError('ACCESS_INPUT_INVALID')
    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique,
                      parse_constant=reject)


def acquire(request, download_dir=None, *, download_only=False, confirm_external_embedding=True,
            api=None, zotero_client=None, cycle_runner=None, configuration=None, **options):
    """Acquisition plus scoped filing/local parsing/index continuation; no Dify."""
    if type(download_only) is not bool or type(confirm_external_embedding) is not bool:
        raise ValueError('WORKFLOW_FLAGS_INVALID')
    scripts = str(ROOT.parent / 'RAGFlow/scripts')
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from workflow_configuration import DEFAULT_CONFIGURATION, read_configuration, workflow_settings
    config_path = Path(configuration).resolve() if configuration is not None else DEFAULT_CONFIGURATION
    config = read_configuration(config_path) if configuration is not None else None
    settings = workflow_settings(config or {}, config_path, legacy_defaults=configuration is None)
    download_dir = Path(download_dir) if download_dir is not None else settings['download_dir']
    if configuration is not None and options.get('execute') and not download_only and confirm_external_embedding and settings['embedding_enabled']:
        primary = config.get('material_targets', {}).get('main_pdf')
        if not primary or not any(source.get('confirmed') is True and
                all(source.get(key) == primary.get(key) and primary.get(key) for key in ('dataset_id', 'base_url', 'state_dir'))
                for source in config.get('sources', [])):
            raise ValueError('MAIN_LIBRARY_TARGET_UNCONFIRMED')
    from acquisition_gateway import acquire as run_acquisition
    result = run_acquisition(request, download_dir, **options)
    result['dify_status'] = 'not_requested'
    if not options.get('execute') or download_only:
        return result
    main = result.get('stages', {}).get('main_pdf', {})
    if main.get('status') not in ('downloaded', 'exists') or not main.get('job_key'):
        return result
    # Partial supplement delivery does not discard a verified main PDF. The
    # same bundle records each missing/unsupported material independently.
    tokenizer_runtime = default_path('tokenizer_runtime')
    if tokenizer_runtime is not None:
        sys.path.append(str(tokenizer_runtime))
    if config is None:
        config = read_configuration(config_path)
        settings = workflow_settings(config, config_path, legacy_defaults=True)
    confirm_external_embedding = confirm_external_embedding and settings['embedding_enabled']
    target = config.get('material_targets', {}).get('main_pdf')
    if not target:
        result.update(status='partial', processing_reason='MAIN_LIBRARY_TARGET_NOT_CONFIGURED')
        return result
    if cycle_runner is None:
        from run_research_update_cycle import run_update_cycle
        cycle_runner = run_update_cycle
    try:
        if confirm_external_embedding and api is None:
            from zotero_scope_processing import runtime_credentials
            from ragflow_prechunk_import import RagflowApi
            key, _ = runtime_credentials(container_name=settings['credential_container'], require_retrieval_token=False)
            api = RagflowApi(target['base_url'], key, transport_base_url=settings['transport_base_url'])
            del key
        cycle = cycle_runner(Path(download_dir), settings['bundle_root'], settings['prepared_root'],
            Path(target['state_dir']), target['base_url'], target['dataset_id'], maximum=1,
            job_keys=frozenset({main['job_key']}), with_zotero=True, execute_zotero=True,
            confirm_zotero_write=True, zotero_client=zotero_client, execute_local=True,
            execute_ragflow=confirm_external_embedding, confirm_ragflow_write=confirm_external_embedding,
            api=api, with_materials=request.get('include_supplements', True), auto_cleanup=False,
            tokenizer_json=settings['tokenizer_json'],
            **({'configuration': config_path} if configuration is not None else {}))
    except (ValueError, OSError, RuntimeError) as error:
        # Original stage receipts remain authoritative. Do not disguise a
        # processing failure as a failed acquisition or retry a write here.
        reason = str(error)
        # Project stage codes are safe to show; unexpected transport messages
        # may contain URLs and remain in their original protected checkpoint.
        code = reason if re.fullmatch(r'[A-Z][A-Z0-9_]{1,160}', reason) else 'PROCESSING_FAILED_SEE_STAGE_RECEIPTS'
        result.update(status='partial', processing_reason=code, processing_error_type=type(error).__name__,
                      processing_status='needs_reconciliation')
        return result
    result['processing'] = cycle
    result['zotero'] = cycle.get('zotero_stage', {})
    result['parsing'] = cycle.get('local_stage', {})
    result['ragflow'] = cycle.get('ragflow_stage', {})
    result['embedding_authorization'] = ('configuration_opt_in' if configuration is not None else
        local_setting('embedding_authorization', 'local_profile_opt_in')) if confirm_external_embedding else 'index_explicitly_disabled'
    materials = cycle.get('material_stage', [])
    if (not confirm_external_embedding or cycle.get('failures') or cycle.get('paused_requires_review')
            or any(m.get('status') != 'complete' for m in materials)
            or request.get('include_supplements', True) and not materials):
        result['status'] = 'partial'
    else:
        result['status'] = 'indexed' if result['status'] == 'saved' else 'partial'
    return result


def capabilities(registry):
    """Describe this public coordinator without constructing service clients."""
    from acquisition_gateway import capabilities as acquisition_capabilities
    result = acquisition_capabilities(registry)
    result.update({
        'downstream': 'execute continues the verified SAVED job through Zotero, local preparation and configured indexing',
        'default_mode': 'preview',
        'preview_effects': {'network': False, 'credential_reads': False, 'writes': False},
        'execute_effects': ['download', 'zotero_write', 'local_parse', 'ragflow_write', 'embedding'],
        'exceptions': {'download_only': 'stop after acquisition',
                       'no_index': 'continue filing/local preparation without indexing',
                       'include_supplements_false': 'main PDF only'},
        'parsing': {'document_formats': ['pdf', 'docx'],
                    'limitations': ['supplement_pdf_requires_source_bound_text_review',
                                    'docx_complex_structures_may_require_review'],
                    'legacy_doc': 'pending_adapter', 'video': 'deferred'},
        'model_authorization': {'standing': ['embedding'] if local_setting('embedding_enabled', False) else [], 'standing_scope': 'existing_local_profile',
                                'report_estimated_cost': True, 'dify_default': False},
        'configuration': {'cli_flag': '--configuration', 'relative_paths': 'configuration_directory',
                          'explicit_profile_defaults': {'embedding_enabled': False, 'credential_source': 'environment'},
                          'indexing_environment_keys': ['RAGFLOW_API_KEY'],
                          'retrieval_environment_keys': ['RAGFLOW_API_KEY', 'RETRIEVAL_API_TOKEN'],
                          'partial_environment': 'stop_without_mixing_container_credentials'},
        'completion': 'inspect per-stage receipts; saved/uploaded does not prove parsed/indexed',
        'unknown_result': 'reconcile the existing job, bundle and remote state before retry',
    })
    return result


def main(argv=None, *, stdin=None):
    parser = QuietParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True, parser_class=QuietParser)
    preview = sub.add_parser('preview', help='One DOI, offline routing only')
    preview.add_argument('--doi', required=True)
    preview.add_argument('--registry', type=Path)
    preview.add_argument('--metadata-file', type=Path)
    existing = sub.add_parser('existing-check', help='Read-only title-candidate duplicate barrier')
    existing.add_argument('--doi', required=True)
    existing.add_argument('--title', required=True)
    existing.add_argument('--download-dir', type=Path, default=default_path('download_dir', 'data/papers'))
    acquisition = sub.add_parser('acquire', help='Preview one paper; execute acquires, files, parses and indexes supported documents',
        description='One scoped main-PDF and supplement request. --execute continues the verified SAVED job through Zotero, local parsing and configured indexing; no Dify. Inspect partial-stage receipts before retrying.')
    acquisition.add_argument('--request', type=Path, required=True)
    acquisition.add_argument('--download-dir', type=Path, help='Override configured download_dir; omitted uses workflow settings')
    acquisition.add_argument('--configuration', type=Path,
                             help='Explicit targets/workflow JSON; relative paths use its directory, environment credentials and indexing off by default')
    acquisition.add_argument('--execute', action='store_true', help='Allow the scoped acquisition and downstream writes; omitted means read-only preview')
    acquisition.add_argument('--confirm-download', action='store_true')
    acquisition.add_argument('--confirm-publisher', action='store_true')
    acquisition.add_argument('--noncommercial-research', action='store_true')
    acquisition.add_argument('--download-only', action='store_true', help='Explicitly stop after acquiring files')
    acquisition.add_argument('--confirm-external-embedding', action='store_true',
                             default=True, help='Local-profile compatibility flag; explicit configurations still require embedding_enabled=true')
    acquisition.add_argument('--no-index', dest='confirm_external_embedding', action='store_false',
                             help='Explicitly stop after filing and local preparation')
    sub.add_parser('capabilities', help='Read-only capability/effect descriptions; no service or model calls')
    sub.add_parser('institution-record', help='Eight observed fields as JSON on stdin; stdout only')
    try:
        args = parser.parse_args(argv)
        if args.mode in ('acquire', 'capabilities'):
            if args.mode == 'capabilities':
                result = capabilities(json.loads((ROOT / 'publisher_registry.json').read_text(encoding='utf-8')))
            else:
                result = acquire(load_acquisition(args.request), args.download_dir,
                    execute=args.execute, confirm_download=args.confirm_download,
                    confirm_publisher=args.confirm_publisher, noncommercial_research=args.noncommercial_research,
                    download_only=args.download_only, confirm_external_embedding=args.confirm_external_embedding,
                    configuration=args.configuration)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get('status') in (None, 'preview', 'saved', 'indexed') else 2
        if args.mode == 'existing-check':
            from existing_pdf_preflight import check_existing_title_pdf
            result = check_existing_title_pdf(args.download_dir, doi=args.doi, title=args.title)
            print(json.dumps({'mode': 'existing-pdf-preflight', **result,
                              'network_requests': 0, 'credential_reads': 0,
                              'writes': False, 'content_identity_validated': False,
                              'live_acceptance': False}, ensure_ascii=False, sort_keys=True))
            return 2 if result['status'] == 'review_required' else 0
        if args.mode == 'preview':
            from publisher_router import normalize_doi
            from fetch_by_doi import main as publisher_main
            doi = normalize_doi(args.doi)
            child_args = ['--dois', doi, '--preview']
            if args.registry is not None:
                child_args += ['--registry', str(args.registry)]
            if args.metadata_file is not None:
                child_args += ['--metadata-file', str(args.metadata_file)]
            return publisher_main(child_args)
        source = sys.stdin if stdin is None else stdin
        raw = source.read(INPUT_LIMIT + 1)
        record = build_observation(raw)
        print(json.dumps({'mode': 'institution-observation-only', 'record': record,
                          'network_requests': 0, 'credential_reads': 0,
                          'downloads': 0, 'writes': False, 'live_acceptance': False},
                         ensure_ascii=False, sort_keys=True))
        return 0
    except ValueError as error:
        code = str(error)
        if code not in ('WORKFLOW_CONFIGURATION_INVALID', 'MAIN_LIBRARY_TARGET_UNCONFIRMED'):
            code = 'ACCESS_INPUT_INVALID'
        print(json.dumps({'status': 'stopped', 'reason_code': code}))
        return 2
    except Exception:
        print(json.dumps({'status': 'stopped', 'reason_code': 'ACCESS_INPUT_INVALID'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
