"""Public full-text link validation; reject signatures and credential queries."""
import re
from urllib.parse import urlsplit, unquote
from publishers.base import _validated_https_url

PUBLIC_QUERIES = {'', 'pdf=render', 'download=1', 'download=true'}
EUROPEPMC_PDF_API = 'https://europepmc.org/api/getPdf'


def europepmc_pdf_api_url(record):
    """Build the public PDF route only from a matched OA core record.

    The caller owns DOI/PMID identity matching. A PMCID alone, free-to-read
    link, or hasPDF flag alone is insufficient. No requests or fallback here.
    """
    if (not isinstance(record, dict) or record.get('isOpenAccess') != 'Y'
            or record.get('hasPDF') != 'Y' or record.get('source') not in {'MED', 'PMC'}):
        return None
    pmcid = record.get('pmcid')
    if type(pmcid) is not str or not re.fullmatch(r'PMC[1-9][0-9]{0,15}', pmcid):
        return None
    return EUROPEPMC_PDF_API + '?pmcid=' + pmcid


def validated_public_fulltext_url(value):
    if (type(value) is not str or not value.isascii() or '#' in value or '\\' in value
            or re.search(r'%(?![0-9a-fA-F]{2})', value)):
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname.lower() if parsed.hostname else ''
        if (not host or '.' not in host or host.endswith(('.lan', '.home'))
                or host.endswith('.') or len(host) > 253
                or (parsed.query not in PUBLIC_QUERIES and not re.fullmatch(
                    r'https://europepmc\.org/api/getPdf\?pmcid=PMC[1-9][0-9]{0,15}', value))
                or parsed.netloc.endswith(':') or ('?' in value and not parsed.query)
                or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                       for label in host.split('.'))):
            return None
        decoded = unquote(parsed.path, errors='strict')
        if any(ord(c) <= 32 or ord(c) == 127 or c == '\\' for c in decoded):
            return None
        return _validated_https_url(value, allowed_hosts={host}, allow_query=bool(parsed.query),
                                    allowed_query=parsed.query or None)
    except (ValueError, UnicodeError):
        return None
