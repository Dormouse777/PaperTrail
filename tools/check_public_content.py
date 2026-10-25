"""Bounded source-content checks; no credential reads, network or file changes.

Reports locations/rules only. This is not a history scanner or proof that every
possible secret, ownership issue or service dependency has been discovered.
"""
import argparse
import json
import os
from pathlib import Path
import re

SKIP = {'.git', '.venv', 'venv', '__pycache__', 'build', 'dist', 'node_modules'}
TEXT = {'.py', '.json', '.md', '.yml', '.yaml', '.toml', '.txt', '.ps1', '.cmd'}
RULES = {
    'absolute_windows_path': re.compile(r'\b[A-Za-z]:[\\/]'),
    'personal_standing_grant': re.compile(r'standing_user_grant_[0-9]{8}'),
    'private_key': re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----'),
    'provider_token': re.compile(r'\b(?:sk-|hf_|gh[pousr]_|github_pat_)[A-Za-z0-9_-]{20,}\b'),
    'signed_query': re.compile(r'(?i)[?&](?:api[_-]?key|access[_-]?token|sig|signature)=[A-Za-z0-9%+/_-]{16,}'),
    'literal_credential': re.compile(r'''(?i)(?:api[_-]?key|password|access[_-]?token)\s*[:=]\s*["'][A-Za-z0-9+/_=-]{20,}["']'''),
}


def scan(root):
    root = Path(root).resolve(strict=True)
    findings, checked = [], 0
    for folder, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(name for name in directories if name not in SKIP and not name.endswith('.egg-info'))
        for name in list(directories):
            path = Path(folder) / name
            if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
                findings.append({'path': path.relative_to(root).as_posix(), 'rule': 'linked_directory'})
                directories.remove(name)
        for name in sorted(files):
            path = Path(folder) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                findings.append({'path': relative, 'rule': 'linked_file'})
                continue
            if ((name.startswith('.env') and name != '.env.example') or name in {'workflow.local.json', 'request.local.json', 'bundle.json'}
                    or name.startswith('.paper-') or path.suffix.lower() in {'.sqlite', '.db', '.zip'}):
                findings.append({'path': relative, 'rule': 'private_runtime_file'})
            if path.suffix.lower() == '.pdf' and relative != 'src/papertrail/fixtures/synthetic.pdf':
                findings.append({'path': relative, 'rule': 'unreviewed_pdf'})
            if path.suffix.lower() not in TEXT and name not in {'.gitignore', 'LICENSE'}:
                continue
            if path.stat().st_size > 4 * 1024 * 1024:
                findings.append({'path': relative, 'rule': 'oversized_text_requires_review'})
                continue
            try:
                lines = path.read_text('utf-8-sig').splitlines()
            except UnicodeError:
                findings.append({'path': relative, 'rule': 'unreadable_text'})
                continue
            checked += 1
            for number, line in enumerate(lines, 1):
                for rule, pattern in RULES.items():
                    if pattern.search(line):
                        findings.append({'path': relative, 'line': number, 'rule': rule})
    return {'text_files_checked': checked, 'findings': findings, 'clean_under_rules': not findings,
            'scope': 'current source files; no Git history, binary-content or legal clearance claim'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    args = parser.parse_args()
    report = scan(args.root)
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report['clean_under_rules'] else 1)
