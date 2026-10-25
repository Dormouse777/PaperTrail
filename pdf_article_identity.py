"""Bounded local article-identity checks, not a proof of full-text completeness."""
from io import BytesIO
import re
import unicodedata

from publisher_router import normalize_doi

MAX_PDF_BYTES = 64 * 1024 * 1024
MAX_TEXT_CHARS = 200000
ARTICLE_IDENTITY_CODES = frozenset({
    'ARTICLE_DOI_INVALID', 'ARTICLE_TITLE_REQUIRED', 'ARTICLE_PDF_UNREADABLE',
    'ARTICLE_TEXT_LIMIT', 'ARTICLE_TEXT_UNAVAILABLE', 'ARTICLE_SUPPLEMENT_NOT_MAIN_TEXT',
    'ARTICLE_COVER_NOT_MAIN_TEXT', 'ARTICLE_DOI_NOT_CONFIRMED', 'ARTICLE_TITLE_NOT_CONFIRMED',
    'ARTICLE_PDF_CHANGED',
})


class ArticleIdentityError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def expected_identity(doi, title):
    try:
        doi = normalize_doi(doi)
    except (ValueError, TypeError):
        raise ArticleIdentityError('ARTICLE_DOI_INVALID') from None
    if (type(title) is not str or not title.strip() or len(title) > 2048
            or any(unicodedata.category(c) in {'Cc', 'Cf', 'Cs'} for c in title)):
        raise ArticleIdentityError('ARTICLE_TITLE_REQUIRED')
    if not compact(title):
        raise ArticleIdentityError('ARTICLE_TITLE_REQUIRED')
    return doi, title.strip()


def compact(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', value).casefold() if c.isalnum())


def verify_article_identity(data, *, doi, title):
    """Use body text of physical pages 1-2; metadata/filename is not proof.

    Missing text/identity fails to review instead of silently enabling OCR,
    network or a new source. One-page articles are not rejected by page count.
    """
    doi, title = expected_identity(doi, title)
    if type(data) is not bytes or not 1024 <= len(data) <= MAX_PDF_BYTES:
        raise ArticleIdentityError('ARTICLE_PDF_UNREADABLE')
    try:
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(data), strict=True)
        if reader.is_encrypted or not reader.pages:
            raise ValueError('unreadable')
        parts = []
        for page in reader.pages[:2]:
            value = page.extract_text() or ''
            if len(value) > MAX_TEXT_CHARS:
                raise ArticleIdentityError('ARTICLE_TEXT_LIMIT')
            parts.append(value)
        text = '\n'.join(parts)
        if len(text) > MAX_TEXT_CHARS:
            raise ArticleIdentityError('ARTICLE_TEXT_LIMIT')
    except ArticleIdentityError:
        raise
    except Exception:
        raise ArticleIdentityError('ARTICLE_PDF_UNREADABLE') from None
    if not text.strip():
        raise ArticleIdentityError('ARTICLE_TEXT_UNAVAILABLE')
    # Reject explicit resource labels, not any incidental mention in prose.
    if re.search(r'(?im)^\s*(?:supporting information|(?:electronic\s+)?supplementary '
                 r'(?:information|materials?|data)|supplemental (?:information|materials?|data))\b', text[:1500]):
        raise ArticleIdentityError('ARTICLE_SUPPLEMENT_NOT_MAIN_TEXT')
    if re.search(r'(?im)^\s*(?:(?:inside\s+)?(?:front|back) cover|cover '
                 r'(?:image|picture|feature))\b', text[:1500]):
        raise ArticleIdentityError('ARTICLE_COVER_NOT_MAIN_TEXT')
    clean = text.replace('\u00ad', '').replace('\u200b', '')
    tokens = {m.group().rstrip('.,;').lower() for m in
              re.finditer(r'10\.\d{4,9}/[A-Z0-9._;()/:+\-]+', clean, re.I)}
    # bioRxiv footer text objects can be extracted in reverse visual order:
    # https://doi.org/<DOI>doi: bioRxiv preprint. The explicit trailing label
    # is a separator, not part of the DOI. Keep ordinary tokens untouched and
    # require the exact footer context before accepting this alternate token.
    tokens.update(m.group(1).lower() for m in re.finditer(
        r'https?://(?:dx\.)?doi\.org/(10\.\d{4,9}/[A-Z0-9._;()/:+\-]+?)'
        r'doi:\s+bioRxiv\s+preprint\b', clean, re.I))
    if doi not in tokens:
        raise ArticleIdentityError('ARTICLE_DOI_NOT_CONFIRMED')
    if compact(title) not in compact(clean):
        raise ArticleIdentityError('ARTICLE_TITLE_NOT_CONFIRMED')
    return {'identity_checked': True, 'physical_pages_checked': len(parts),
            'fulltext_completeness_proven': False}
