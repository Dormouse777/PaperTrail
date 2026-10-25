"""Local publisher API connection checklist. Never probes APIs or reads secrets."""
import argparse
import importlib.util
import json
from pathlib import Path

from fetch_by_doi import ROOT, _load
from publishers import create_adapter
from publishers.base import AdapterFailure
from publisher_router import normalize_doi


CHANNELS = {'elsevier': ('ELSEVIER_API_KEY', 'pdf', '10.1016/setup.fixture'),
            'wiley': ('WILEY_TDM_TOKEN', 'pdf', '10.1002/setup.fixture'),
            'springer_nature': ('SPRINGER_NATURE_API_KEY', 'jats_xml', '10.1038/setup.fixture')}


def validate_test_selection(registry, channel, doi):
    """Validate a single selected channel before any interactive key prompt."""
    if channel not in CHANNELS or not isinstance(doi, str) or ',' in doi:
        raise ValueError('PUBLISHER_TEST_SELECTION_INVALID')
    normalized = normalize_doi(doi)
    entry = registry.get(channel)
    prefixes = entry.get('doi_prefixes') if type(entry) is dict else None
    if (type(prefixes) is not list or not prefixes
            or any(not isinstance(p, str) for p in prefixes)
            or normalized.split('/', 1)[0] not in prefixes):
        raise ValueError('PUBLISHER_TEST_CHANNEL_DOI_MISMATCH')
    return {'channel': channel, 'doi': normalized,
            'credential_variable': CHANNELS[channel][0],
            'resource_type': CHANNELS[channel][1]}


def check_setup(registry, *, credential_presence=None, wiley_redirect_doi=None):
    """Optional booleans are supplied by the caller, never discovered here.

    `configured_for_live_test` means local gates are configured, not that an
    account, license, host, or remote API has been verified by this checker.
    """
    if type(registry) is not dict:
        raise ValueError('REGISTRY_INVALID')
    if wiley_redirect_doi is not None:
        wiley_redirect_doi = validate_test_selection(registry, 'wiley', wiley_redirect_doi)['doi']
    if credential_presence is not None and (
            type(credential_presence) is not dict or not set(credential_presence).issubset(CHANNELS)
            or any(type(v) is not bool for v in credential_presence.values())):
        raise ValueError('CREDENTIAL_PRESENCE_FLAGS_INVALID')
    channels = []
    parser_available = importlib.util.find_spec('pypdf') is not None
    for name, (variable, resource, doi) in CHANNELS.items():
        entry = registry.get(name)
        missing = []
        policy_valid = False
        if type(entry) is not dict:
            missing.append('publisher_registry_entry')
        else:
            if entry.get('credential_env') != variable:
                missing.append('credential_variable_mismatch')
            # A synthetic DOI validates static routing policy only. No reader,
            # environment, output directory, HTTP, or governor is consulted.
            adapter = create_adapter(name, entry, credential_reader=lambda _: None)
            try:
                if name == 'springer_nature':
                    adapter._jats_policy(doi)
                else:
                    adapter._subscription_policy({'publisher': name}, doi)
                policy_valid = True
            except AdapterFailure as failure:
                missing.append(failure.code)
            except Exception:
                missing.append('PUBLISHER_POLICY_INVALID')
            if entry.get('license_status') != 'approved':
                missing.append('approved_publisher_permission')
            if not entry.get('permission_evidence'):
                missing.append('account_permission_evidence')
            if name == 'springer_nature' and entry.get('jats_product_verified') is not True:
                missing.append('jats_product_verification')
            if name == 'wiley' and wiley_redirect_doi is None:
                try:
                    adapter.validated_binary_hosts()
                except Exception:
                    missing.append('approved_exact_binary_hosts_and_policy')
        if resource == 'pdf' and not parser_available:
            missing.append('pypdf_dependency')
        presence = None if credential_presence is None else credential_presence.get(name)
        if presence is not True:
            missing.append(variable)
        channels.append({'channel': name, 'resource_type': resource,
            'credential_variable': variable, 'credential_presence': 'not_checked' if presence is None else presence,
            'missing': list(dict.fromkeys(missing)),
            'configured_for_live_test': policy_valid and not missing,
            'live_acceptance': False})
        if name == 'wiley' and wiley_redirect_doi is not None:
            channels[-1]['binary_redirect_scope'] = {'mode': 'official_api_directed_single_doi',
                'doi': wiley_redirect_doi, 'max_api_requests': 1, 'max_anonymous_hops': 1,
                'target_host_verified': False, 'long_term_allowlist_changed': False}
    return {'mode': 'local-setup-check', 'channels': channels,
        'pdf_parser_available': parser_available,
        'network_requests': 0, 'credential_reads': 0, 'writes': False,
        'automatic_channels': list(CHANNELS), 'manual_only_channels': ['aaas', 'unknown'],
        'institution_sessions_reused': False, 'live_acceptance': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', type=Path, default=ROOT / 'publisher_registry.json')
    parser.add_argument('--channel', choices=tuple(CHANNELS))
    parser.add_argument('--doi')
    parser.add_argument('--wiley-api-redirect', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.wiley_api_redirect and (args.channel != 'wiley' or not args.doi):
            raise ValueError('WILEY_SINGLE_ARTICLE_SCOPE_INVALID')
        registry, sha = _load(args.registry)
        result = check_setup(registry, wiley_redirect_doi=args.doi if args.wiley_api_redirect else None)
        if args.channel is not None or args.doi is not None:
            result['test_selection'] = validate_test_selection(registry, args.channel, args.doi)
        result['registry_sha256'] = sha
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception:
        print(json.dumps({'status': 'stopped', 'reason_code': 'PUBLISHER_SETUP_REVIEW_REQUIRED'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
