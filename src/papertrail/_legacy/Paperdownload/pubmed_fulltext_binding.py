"""Bind a selected fulltext candidate to fresh Europe PMC PubMed metadata.

Pure projection: no requests, file writes, credentials or access-right inference.
Only public bibliography is returned; fulltext URLs and abstracts are discarded.
"""
import re
import unicodedata

from fulltext_candidates import _validate_tree
from publisher_router import normalize_doi


def _text(value, limit=2048):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(unicodedata.category(c) in {'Cc', 'Cf', 'Cs'} for c in value)):
        raise ValueError('FULLTEXT_PUBMED_METADATA_INVALID')
    return value


def _title_key(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', _text(value)).casefold()
                   if c.isalnum())


def pubmed_paper_from_core(response, *, doi, pmid, title):
    """Require a complete response with one MED record for the selected DOI.

    PMID is supplied, never guessed. Matching title punctuation/case may differ;
    the original Europe PMC title is retained. OA status is not authorization.
    All invalid inputs raise fixed codes without echoing the response.
    """
    try:
        doi = normalize_doi(doi)
        if (not isinstance(pmid, str) or re.fullmatch(r'[0-9]{1,16}', pmid) is None
                or not _title_key(title)):
            raise ValueError('FULLTEXT_PUBMED_SCOPE_INVALID')
        _validate_tree(response)
        if (not isinstance(response, dict) or 'error' in response or 'errCode' in response
                or type(response.get('hitCount')) is not int
                or not isinstance(response.get('resultList'), dict)):
            raise ValueError('FULLTEXT_PUBMED_METADATA_INVALID')
        records = response['resultList'].get('result')
        if (not isinstance(records, list) or not 1 <= len(records) <= 100
                or response['hitCount'] != len(records)):
            raise ValueError('FULLTEXT_PUBMED_RESPONSE_INCOMPLETE')
        matches = []
        for record in records:
            if not isinstance(record, dict):
                raise ValueError('FULLTEXT_PUBMED_METADATA_INVALID')
            if record.get('source') == 'MED' and record.get('doi') is not None:
                if normalize_doi(record['doi']) == doi:
                    matches.append(record)
        if len(matches) != 1 or matches[0].get('id') != pmid:
            raise ValueError('FULLTEXT_PUBMED_IDENTITY_MISMATCH')
        record = matches[0]
        actual_title = _text(record.get('title'))
        if _title_key(actual_title) != _title_key(title):
            raise ValueError('FULLTEXT_PUBMED_TITLE_MISMATCH')
        paper = {'source': 'pubmed', 'id': pmid, 'doi': doi, 'title': actual_title}
        if record.get('pmcid'):
            pmcid = _text(record['pmcid'], 32)
            if re.fullmatch(r'PMC[0-9]{1,16}', pmcid) is None:
                raise ValueError('FULLTEXT_PUBMED_METADATA_INVALID')
            paper['pmcid'] = pmcid
        if record.get('firstPublicationDate'):
            paper['published'] = _text(record['firstPublicationDate'], 64)
        journal_info = record.get('journalInfo')
        if journal_info is not None:
            if not isinstance(journal_info, dict):
                raise ValueError('FULLTEXT_PUBMED_METADATA_INVALID')
            journal = journal_info.get('journal')
            if journal is not None:
                if not isinstance(journal, dict):
                    raise ValueError('FULLTEXT_PUBMED_METADATA_INVALID')
                if journal.get('title'):
                    paper['journal'] = _text(journal['title'], 512)
            if 'published' not in paper and journal_info.get('printPublicationDate'):
                paper['published'] = _text(journal_info['printPublicationDate'], 64)
        author_list = record.get('authorList')
        if author_list is not None:
            if not isinstance(author_list, dict) or not isinstance(author_list.get('author'), list):
                raise ValueError('FULLTEXT_PUBMED_METADATA_INVALID')
            authors = author_list['author']
            if len(authors) > 256 or any(not isinstance(a, dict) for a in authors):
                raise ValueError('FULLTEXT_PUBMED_METADATA_INVALID')
            paper['authors'] = [_text(a.get('fullName'), 512) for a in authors]
        elif record.get('authorString'):
            # Europe PMC's authorString is comma-separated, retaining its names.
            names = _text(record['authorString'], 16384).split(',')
            if len(names) > 256:
                raise ValueError('FULLTEXT_PUBMED_METADATA_INVALID')
            paper['authors'] = [_text(name.strip(), 512) for name in names]
        return paper
    except (ValueError, TypeError, RecursionError):
        # Do not carry arbitrary text from normalize_doi or caller objects.
        raise ValueError('FULLTEXT_PUBMED_BINDING_REJECTED') from None
