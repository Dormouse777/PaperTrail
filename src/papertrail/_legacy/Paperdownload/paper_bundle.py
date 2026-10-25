"""Per-paper storage, verified manifests, and acquisition completion receipts."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from urllib.parse import quote

from download_ledger import identity_keys, verify_pdf
from download_ledger import normalized_doi
from workflow_defaults import default_path

DEFAULT_PAPER_DIRECTORY = default_path('download_dir', 'data/papers')


class BundleError(ValueError):
    pass


def _check_identity_fields(existing, incoming):
    for field, normalize in (('doi', normalized_doi), ('pmcid', lambda v: str(v).strip().upper())):
        if existing.get(field) and incoming.get(field):
            if normalize(existing[field]) != normalize(incoming[field]):
                raise BundleError('BUNDLE_PAPER_IDENTITY_CONFLICT')
    if existing.get('source') and incoming.get('source'):
        if existing['source'] != incoming['source']:
            raise BundleError('BUNDLE_PAPER_IDENTITY_CONFLICT')
        if existing.get('id') and incoming.get('id') and str(existing['id']).lower() != str(incoming['id']).lower():
            raise BundleError('BUNDLE_PAPER_IDENTITY_CONFLICT')


def _regular_chain(path):
    for entry in (path, *path.parents):
        if entry.is_symlink() or (entry.exists() and getattr(entry.stat(), 'st_file_attributes', 0) & 0x400):
            raise BundleError('BUNDLE_LINK_PATH_REJECTED')


def bundle_folder_name(paper):
    """Use a publication identifier, independent of mutable article titles."""
    keys = identity_keys(paper)
    if keys:
        # identity_keys orders DOI, PMID, PMCID, arXiv. Percent encoding keeps
        # DOI slashes/Windows-forbidden characters reversible and collision-free.
        key = keys[0]
        kind, value = key.split(':', 1)
        encoded = quote(value, safe='-_')
        if encoded.endswith('.'):
            encoded = encoded.rstrip('.') + '%2E' * (len(encoded) - len(encoded.rstrip('.')))
        name = kind.upper() + '_' + encoded
        if len(name) > 80:
            name = kind.upper() + '_' + hashlib.sha256(key.encode()).hexdigest()[:24]
        return name
    label = json.dumps([paper.get('id'), paper.get('title')], ensure_ascii=True)
    return 'LOCAL_' + hashlib.sha256(label.encode()).hexdigest()[:24]


def ensure_bundle(root, paper, *, create=True):
    root = Path(root).absolute()
    _regular_chain(root)
    keys = identity_keys(paper)
    if not keys:
        # Legacy callers can lack source identifiers; do not invent a DOI/PMID.
        # Their ledger continues to use the saved file's SHA256 identity.
        label = json.dumps([paper.get('id'), paper.get('title')], ensure_ascii=True)
        keys = ['local:' + hashlib.sha256(label.encode()).hexdigest()]
    folder = root / bundle_folder_name(paper)
    _regular_chain(folder)
    if create:
        folder.mkdir(parents=True, exist_ok=True)
    manifest = read_bundle(folder)
    old_keys = identity_keys(manifest.get('paper', {})) if manifest else []
    if manifest:
        _check_identity_fields(manifest['paper'], paper)
    if old_keys and not set(old_keys).intersection(keys):
        raise BundleError('BUNDLE_PAPER_IDENTITY_CONFLICT')
    return folder


def read_bundle(folder):
    path = Path(folder) / 'bundle.json'
    _regular_chain(path)
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise BundleError('BUNDLE_MANIFEST_INVALID')
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise BundleError('BUNDLE_MANIFEST_INVALID') from None
    if (not isinstance(value, dict) or value.get('version') != 1
            or not isinstance(value.get('paper'), dict) or not isinstance(value.get('files'), list)):
        raise BundleError('BUNDLE_MANIFEST_INVALID')
    return value


def write_bundle(folder, manifest):
    folder = Path(folder)
    path = folder / 'bundle.json'
    _regular_chain(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=folder,
                                         prefix='.bundle-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(manifest, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def file_identity(path):
    path = Path(path)
    _regular_chain(path)
    if not path.is_file():
        raise BundleError('BUNDLE_FILE_MISSING')
    sha, md5, size = hashlib.sha256(), hashlib.md5(), 0
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            sha.update(block)
            md5.update(block)
            size += len(block)
    return {'sha256': sha.hexdigest(), 'md5': md5.hexdigest(), 'size_bytes': size}


def record_main_pdf(root, paper, pdf_path, source=None, license_value=None):
    root, pdf = Path(root).absolute(), Path(pdf_path).absolute()
    _regular_chain(pdf)
    if not pdf.is_relative_to(root) or pdf.parent == root:
        # Historical flat files retain their existing ledger and are not migrated.
        return {}
    verify_pdf(pdf)
    manifest = read_bundle(pdf.parent) or {'version': 1, 'paper': {}, 'files': [],
                                          'supplements': {'status': 'not_checked'}}
    incoming = {k: paper[k] for k in ('doi', 'pmcid', 'source', 'id', 'title', 'published',
                'supplements_oa_eligible', 'supplements_license') if paper.get(k) is not None}
    old_keys = identity_keys(manifest['paper'])
    _check_identity_fields(manifest['paper'], incoming)
    if old_keys and not set(old_keys).intersection(identity_keys(paper)):
        raise BundleError('BUNDLE_PAPER_IDENTITY_CONFLICT')
    manifest['paper'].update(incoming)
    entry = {'relative_path': pdf.name, 'filename': pdf.name, 'role': 'main_pdf',
             'content_type': 'application/pdf', **file_identity(pdf),
             'source': source or paper.get('source'), 'license': license_value or paper.get('license')}
    mains = [r for r in manifest['files'] if isinstance(r, dict) and r.get('role') == 'main_pdf']
    if mains and any(r.get('relative_path') != pdf.name or r.get('sha256') != entry['sha256'] for r in mains):
        raise BundleError('BUNDLE_MAIN_PDF_CONFLICT')
    manifest['files'] = [r for r in manifest['files'] if not isinstance(r, dict) or r.get('role') != 'main_pdf'] + [entry]
    write_bundle(pdf.parent, manifest)
    return {'bundle_path': str(pdf.parent / 'bundle.json'), 'paper_directory': str(pdf.parent)}


def verified_supplements(pdf_path):
    pdf = Path(pdf_path).absolute()
    manifest = read_bundle(pdf.parent)
    if manifest is None:
        return []
    main = [r for r in manifest['files'] if isinstance(r, dict) and r.get('role') == 'main_pdf']
    actual = file_identity(pdf)
    if (len(main) != 1 or main[0].get('relative_path') != pdf.name
            or main[0].get('sha256') != actual['sha256'] or main[0].get('size_bytes') != actual['size_bytes']):
        raise BundleError('BUNDLE_MAIN_PDF_CHANGED')
    result, seen = [], set()
    for record in manifest['files']:
        if not isinstance(record, dict):
            raise BundleError('BUNDLE_FILE_RECORD_INVALID')
        if record.get('role') != 'supplement':
            continue
        relative = record.get('relative_path')
        if not isinstance(relative, str) or '\\' in relative or ':' in relative:
            raise BundleError('BUNDLE_FILE_PATH_INVALID')
        parts = PurePosixPath(relative)
        if parts.is_absolute() or '..' in parts.parts or len(parts.parts) != 2 or parts.parts[0] != 'supplements':
            raise BundleError('BUNDLE_FILE_PATH_INVALID')
        path = pdf.parent / relative
        if relative.casefold() in seen or path.name != record.get('filename'):
            raise BundleError('BUNDLE_DUPLICATE_FILE')
        seen.add(relative.casefold())
        actual = file_identity(path)
        if record.get('sha256') != actual['sha256'] or record.get('size_bytes') != actual['size_bytes']:
            raise BundleError('BUNDLE_SUPPLEMENT_CHANGED')
        mime = record.get('content_type')
        if not isinstance(mime, str) or not re.fullmatch(r'[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+', mime):
            raise BundleError('BUNDLE_CONTENT_TYPE_INVALID')
        result.append({**record, **actual, 'path': path})
    return result


def finish_download(root, paper, pdf_path, *, governor, source=None, license_value=None):
    """Keep main-PDF success separate from supplement availability/completeness."""
    try:
        bundle = record_main_pdf(root, paper, pdf_path, source=source, license_value=license_value)
        if not bundle:
            return {'supplements_status': 'not_checked'}
    except (BundleError, OSError, ValueError):
        return {'supplements_status': 'partial', 'supplements_error': 'BUNDLE_NEEDS_REVIEW'}
    # This import is lazy so local manifest inspection never requires a transport.
    try:
        from acquisition_gateway import supplements_requested
        if not supplements_requested():
            return {**bundle, 'supplements_status': 'not_requested'}
        from paper_supplements import acquire_supplements
        return {**bundle, **acquire_supplements(paper, pdf_path, governor=governor)}
    except Exception:
        # A valid saved main PDF stays available even when supplement acquisition
        # fails; never claim the complete literature bundle was acquired.
        return {**bundle, 'supplements_status': 'partial', 'supplements_error': 'SUPPLEMENT_ACQUISITION_FAILED'}
