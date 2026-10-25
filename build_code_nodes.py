"""Build reviewed self-contained sandbox code; never call or configure Dify."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _inline_source(filename, dependency):
    helper = (ROOT / "evidence_source_metadata.py").read_text(encoding="utf-8")
    source = (ROOT / filename).read_text(encoding="utf-8")
    if source.count(dependency) != 1:
        raise ValueError("Unexpected metadata helper dependency")
    return helper + "\n" + source.replace(dependency, "")


def build_evidence_source(*, grouped=False):
    source = _inline_source("evidence_gate.py",
        "from evidence_source_metadata import normalize_source_metadata, source_metadata_lines, normalize_docx_source")
    if not grouped:
        return source
    signature = "def main(query, status_code, body):"
    if source.count(signature) != 1:
        raise ValueError("Unexpected evidence gate entrypoint")
    adapter = (ROOT / "grouped_evidence_gate.py").read_text(encoding="utf-8")
    dependency = "from evidence_gate import main as verified_gate"
    if adapter.count(dependency) != 1:
        raise ValueError("Unexpected adapter dependency")
    return source.replace(signature, "def verified_gate(query, status_code, body):") + "\n" + adapter.replace(dependency, "")


def build_citation_source(*, enforce_literal_counts=False):
    source = _inline_source("citation_audit.py", "from evidence_source_metadata import normalize_source_metadata, normalize_docx_source")
    if not enforce_literal_counts:
        return source
    source += '\n\n_literal_citation_main = main\n\n'
    source += (ROOT / 'numeric_source_audit.py').read_text(encoding='utf-8')
    source += '''

def main(answer, citation_manifest, evidence_text):
    result = _literal_citation_main(answer, citation_manifest)
    if result['citation_ok'] and not audit_literal_counts(answer, evidence_text)['count_ok']:
        return {'citation_ok': False, 'verified_answer': COUNT_LITERAL_REFUSAL, 'cited_count': 0}
    return result
'''
    return source
