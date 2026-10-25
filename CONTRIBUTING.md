# Contributing

Keep changes small and tied to an observable behavior. The command facade reuses
the original business modules; do not introduce a second downloader, agent router
or parallel job ledger. Preserve original bytes, stage receipts and unknown-write
reconciliation. Read [workflow contracts](docs/WORKFLOW.md) before changing effects.

Use Python 3.11+ and a separate virtual environment. Install this source tree and
run `python -m unittest discover -s tests -v`. To check installation rather than
source imports, build a wheel with `python -m pip wheel --no-deps . -w dist`,
install that wheel in a new environment, change to another directory, and run the
tests with `python -I -B -m unittest discover -s PATH_TO_THIS_REPOSITORY/tests -v`.
The current local evidence is Windows/Python 3.12.14; other platforms and complete
service integration require separate evidence.

Tests and examples must use self-authored synthetic or redistributable material.
No API keys, browser sessions, private paths, research originals, live target IDs,
bundles, ledgers or service volumes. Real service or paid-model tests are opt-in
and must specify identity, scope, budget and readback; offline CI must not call
them. A mock is not an end-to-end result.

The project's own material is licensed under [MIT](LICENSE). Contribute only
material you have the right to share under the applicable terms. See
[licensing and provenance](LICENSING.md); do not assume the project owns upstream
RAGFlow, Dify, MinerU, Zotero or a model's tokenizer assets. Public hosting is a
separate step; this remains a local publication candidate.
