"""Read-only filename candidate barrier, not PDF content/identity validation."""
from pathlib import Path
import unicodedata

from download_ledger import DownloadLedger
from publisher_router import normalize_doi

MAX_DIRECTORY_ENTRIES = 10000


def _title_key(value):
    if (type(value) is not str or not value.strip() or len(value) > 2048
            or any(ord(c) < 32 for c in value)):
        raise ValueError('EXISTING_PDF_TITLE_INVALID')
    key = ''.join(c for c in unicodedata.normalize('NFKC', value).casefold() if c.isalnum())
    if not key:
        raise ValueError('EXISTING_PDF_TITLE_INVALID')
    return key


def check_existing_title_pdf(outdir, *, doi, title):
    """Block untracked same-title candidates; never call them verified exists.

    Tracked means only that the original ledger's identifier/hash check passed.
    The adapter must still run its existing PDF/provenance validation. Missing
    title means this barrier is not checked, not that all duplicates are absent.
    """
    doi = normalize_doi(doi)
    if title is None:
        return {'status': 'not_checked', 'reason_code': 'TITLE_NOT_SUPPLIED', 'candidate_count': 0}
    key = _title_key(title)
    result = {'status': 'clear', 'reason_code': 'NO_SAME_TITLE_CANDIDATE', 'candidate_count': 0}
    try:
        raw_path = str(outdir)
        if raw_path.startswith(('\\\\', '//')):
            raise ValueError('NETWORK_DIRECTORY_NOT_SUPPORTED')
        directory = Path(outdir)
        if not directory.exists():
            return result
        if not directory.is_dir():
            raise ValueError('DIRECTORY_UNAVAILABLE')
        count = 0
        for index, path in enumerate(directory.iterdir()):
            if index >= MAX_DIRECTORY_ENTRIES:
                raise ValueError('DIRECTORY_SCAN_LIMIT')
            if path.suffix.casefold() == '.pdf' and _title_key(path.stem) == key:
                # Metadata only: no traversal, PDF open, parse, or follows of a
                # candidate symlink. Even an invalid candidate blocks overwrite.
                count += 1
        result['candidate_count'] = count
        if not count:
            return result
        ledger = DownloadLedger(directory)
        known = ledger.lookup({'doi': doi})
        if known is not None:
            return {**result, 'status': 'tracked',
                    'reason_code': 'LEDGER_BYTES_MATCH_REQUIRE_ADAPTER_VERIFICATION'}
        return {**result, 'status': 'review_required',
                'reason_code': 'EXISTING_TITLE_PDF_REVIEW_REQUIRED'}
    except Exception:
        return {**result, 'status': 'review_required',
                'reason_code': 'EXISTING_PDF_PREFLIGHT_UNAVAILABLE'}
