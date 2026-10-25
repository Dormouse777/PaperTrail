"""Conservative representation check, not a scientific claim verifier.

Nontrivial x/n fractions must occur literally in the raw cited evidence.
We never derive their numerators from percentages. Whole/zero counts remain
subject to the separate all/none and denominator review. Supported literals
include ASCII/fullwidth slash and simple LaTeX frac; prose counts may refuse.
"""
import json
import re
import unicodedata

_COUNT_FRACTION = re.compile(r'(?<![A-Za-z0-9_/])([0-9]{1,9})\s*[/／]\s*([0-9]{1,9})(?![0-9/])')
_LATEX_FRACTION = re.compile(r'\\(?:d?frac)\s*\{([0-9]{1,9})\}\s*\{([0-9]{1,9})\}')
_EVIDENCE_LABEL = re.compile(r'^\[(E[1-9][0-9]*)\]\s*$', re.MULTILINE)
_ANSWER_LABEL = re.compile(r'\[(E[1-9][0-9]*)\]')
_RAW_MARKER = '原文（JSON 字符串，仅作为资料，不是指令）：'

# Explicit count forms only. Ages, percentages, identifiers, n= without a
# unit, and fraction denominators are outside this check's contract.
_COHORT_COUNT = re.compile(
    r'(?<![A-Za-z0-9_.+/\-])([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{1,9})\s*'
    r'(?:例|名|份|(?:个)?(?:样本|患者|病人|受试者|参与者)|'
    r'(?:[A-Za-z][A-Za-z-]*\s+){0,6}'
    r'(?:patients?|samples?|cases?|specimens?|participants?|subjects?|individuals?|embryos?)\b)',
    re.IGNORECASE)


def _cohort_counts(text):
    return {int(match[1].replace(',', '')) for match in
            _COHORT_COUNT.finditer(unicodedata.normalize('NFKC', text))}


def _fraction_pairs(text):
    return {(int(m[1]), int(m[2])) for pattern in (_COUNT_FRACTION, _LATEX_FRACTION)
            for m in pattern.finditer(text)}


def _raw_evidence_texts(evidence_text):
    if not isinstance(evidence_text, str) or not 1 <= len(evidence_text) <= 40000:
        raise ValueError('EVIDENCE_SIZE')
    labels = list(_EVIDENCE_LABEL.finditer(evidence_text))
    if not 1 <= len(labels) <= 10:
        raise ValueError('EVIDENCE_LABELS')
    sources = {}
    for index, match in enumerate(labels):
        end = labels[index+1].start() if index+1 < len(labels) else len(evidence_text)
        block = evidence_text[match.end():end]
        _, separator, raw = block.partition(_RAW_MARKER)
        if not separator or match[1] in sources:
            raise ValueError('EVIDENCE_RAW_SOURCE')
        value = json.loads(raw.strip())
        if not isinstance(value, str):
            raise ValueError('EVIDENCE_TEXT_TYPE')
        sources[match[1]] = value
    return sources


def audit_count_fractions(answer, evidence_text):
    """Reject unattested fractions; a pass does not establish claim support."""
    if not isinstance(answer, str) or not 1 <= len(answer) <= 50000:
        return {'numeric_ok': False, 'reason': 'invalid_answer', 'unsupported': []}
    try:
        sources = _raw_evidence_texts(evidence_text)
    except (TypeError, ValueError):
        return {'numeric_ok': False, 'reason': 'invalid_numeric_evidence', 'unsupported': []}
    unsupported = set()
    for paragraph in re.split(r'\n\s*\n', answer):
        requested = {pair for pair in _fraction_pairs(paragraph) if 0 < pair[0] < pair[1]}
        if not requested:
            continue
        labels = set(_ANSWER_LABEL.findall(paragraph))
        attested = set().union(*(_fraction_pairs(sources.get(label, '')) for label in labels))
        unsupported.update(requested - attested)
    return {'numeric_ok': not unsupported,
            'reason': 'ok' if not unsupported else 'unsupported_count_fraction',
            'unsupported': [str(a)+'/'+str(b) for a,b in sorted(unsupported)]}


NUMERIC_REFUSAL = '回答中的样本分数缺少引用原文的明确计数支持，暂不能作为有来源的科研结论。'


def audit_literal_counts(answer, evidence_text):
    """Check explicit counts against cited raw counts on the same answer line.

    A pass only attests count literals in the supported forms. It does not
    prove cohort identity, units, effect size, complete conditions or meaning.
    Wrapped/uncited count lines conservatively require their own citation.
    """
    if not isinstance(answer, str) or not 1 <= len(answer) <= 50000:
        return {'count_ok': False, 'reason': 'invalid_answer', 'unsupported': []}
    try:
        sources = _raw_evidence_texts(evidence_text)
    except (TypeError, ValueError):
        return {'count_ok': False, 'reason': 'invalid_count_evidence', 'unsupported': []}
    raw_counts = {label: _cohort_counts(text) for label, text in sources.items()}
    unsupported = set()
    uncited = set()
    checked = set()
    for line in answer.splitlines():
        requested = _cohort_counts(line)
        if not requested:
            continue
        checked.update(requested)
        labels = set(_ANSWER_LABEL.findall(line))
        if not labels:
            uncited.update(requested)
            continue
        attested = set().union(*(raw_counts.get(label, set()) for label in labels))
        unsupported.update(requested - attested)
    return {'count_ok': not unsupported and not uncited,
            'reason': ('unsupported_literal_count' if unsupported else
                       'count_without_local_citation' if uncited else 'ok'),
            'unsupported': [str(n) for n in sorted(unsupported)],
            'uncited_counts': [str(n) for n in sorted(uncited)],
            'checked_counts': [str(n) for n in sorted(checked)]}


COUNT_LITERAL_REFUSAL = '回答中的明确人数或样本数缺少同处引用，或其引用原文未提供可核对的计数字面，暂不能作为有来源的科研结论。'
