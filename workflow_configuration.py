"""Read workflow settings without creating directories or resolving credentials."""
import copy
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

PROJECT = Path(__file__).resolve().parents[2]
_paper_modules = str(PROJECT / 'Paperdownload')
if _paper_modules not in sys.path:
    sys.path.insert(0, _paper_modules)
from workflow_defaults import default_path, local_setting

DEFAULT_CONFIGURATION = default_path('configuration', 'data/workflow.json')
LEGACY_CREDENTIAL_CONTAINER = local_setting('credential_container')


def configuration_path(value, configuration):
    """Resolve an explicitly configured path relative to its JSON file."""
    if not isinstance(value, (str, Path)) or not str(value).strip() or '\x00' in str(value):
        raise ValueError('WORKFLOW_CONFIGURATION_INVALID')
    path = Path(value).expanduser()
    return (path if path.is_absolute() else Path(configuration).resolve().parent / path).resolve()


def normalize_configuration(config, configuration):
    """Normalize source/state paths in a copy; never rewrite the user's JSON."""
    if not isinstance(config, dict):
        raise ValueError('WORKFLOW_CONFIGURATION_INVALID')
    config = copy.deepcopy(config)
    targets = config.get('material_targets', {})
    sources = config.get('sources', [])
    if not isinstance(targets, dict) or not isinstance(sources, list):
        raise ValueError('WORKFLOW_CONFIGURATION_INVALID')
    for target in [*targets.values(), *sources]:
        if not isinstance(target, dict):
            raise ValueError('WORKFLOW_CONFIGURATION_INVALID')
        if 'state_dir' in target:
            target['state_dir'] = str(configuration_path(target['state_dir'], configuration))
    if 'bundle_paths' in config:
        if not isinstance(config['bundle_paths'], list):
            raise ValueError('WORKFLOW_CONFIGURATION_INVALID')
        config['bundle_paths'] = [str(configuration_path(p, configuration)) for p in config['bundle_paths']]
    return config


def read_configuration(configuration):
    path = Path(configuration)
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('WORKFLOW_CONFIGURATION_INVALID')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('WORKFLOW_CONFIGURATION_INVALID')
            result[key] = value
        return result
    def reject(_):
        raise ValueError('WORKFLOW_CONFIGURATION_INVALID')
    try:
        config = json.loads(path.read_text('utf-8'), object_pairs_hook=unique, parse_constant=reject)
    except (ValueError, UnicodeError):
        raise ValueError('WORKFLOW_CONFIGURATION_INVALID') from None
    return normalize_configuration(config, path)


def workflow_settings(config, configuration, *, legacy_defaults=False):
    values = config.get('workflow', {})
    allowed = {'download_dir', 'bundle_root', 'prepared_root', 'tokenizer_json',
               'transport_base_url', 'credential_source', 'credential_container', 'embedding_enabled'}
    if not isinstance(values, dict) or set(values) - allowed:
        raise ValueError('WORKFLOW_CONFIGURATION_INVALID')
    defaults = {'download_dir': default_path('download_dir', 'data/papers') if legacy_defaults else 'data/papers',
                'bundle_root': default_path('bundle_root', 'data/bundles') if legacy_defaults else 'data/bundles',
                'prepared_root': default_path('prepared_root', 'data/prepared') if legacy_defaults else 'data/prepared',
                'tokenizer_json': default_path('tokenizer_json') if legacy_defaults else None}
    result = {}
    for key, default in defaults.items():
        value = values.get(key, default)
        result[key] = None if key == 'tokenizer_json' and value is None else configuration_path(value, configuration)
    result['embedding_enabled'] = values.get('embedding_enabled', local_setting('embedding_enabled', False) if legacy_defaults else False)
    if type(result['embedding_enabled']) is not bool:
        raise ValueError('WORKFLOW_CONFIGURATION_INVALID')
    source = values.get('credential_source', 'legacy_container' if legacy_defaults and LEGACY_CREDENTIAL_CONTAINER else 'environment')
    if source not in ('environment', 'legacy_container'):
        raise ValueError('WORKFLOW_CONFIGURATION_INVALID')
    container = values.get('credential_container', LEGACY_CREDENTIAL_CONTAINER)
    if source == 'legacy_container':
        import re
        if not isinstance(container, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', container):
            raise ValueError('WORKFLOW_CONFIGURATION_INVALID')
    elif 'credential_container' in values:
        raise ValueError('WORKFLOW_CONFIGURATION_INVALID')
    result['credential_container'] = container if source == 'legacy_container' else None
    result['transport_base_url'] = values.get('transport_base_url',
        local_setting('transport_base_url') if legacy_defaults else None)
    url = result['transport_base_url']
    if url is not None:
        try:
            parsed = urlsplit(url)
            if (not isinstance(url, str) or parsed.scheme not in ('http', 'https') or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment):
                raise ValueError
            parsed.port
        except (ValueError, TypeError, AttributeError):
            raise ValueError('WORKFLOW_CONFIGURATION_INVALID') from None
    return result
