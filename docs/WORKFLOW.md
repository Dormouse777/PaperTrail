# Workflow and interfaces

These procedures describe the existing modules. They do not grant permissions,
provision external services or imply that a demo passed the complete workflow.
Use the configuration and services belonging to the current operator.

## Acquisition

`papertrail capabilities`, `preview --doi DOI`, and `acquire --help` are the
supported discovery commands. The first two are offline. An acquisition request:

```json
{
  "version": 1,
  "paper": {"doi": "10.1000/example", "title": "Synthetic request"},
  "channel": "auto",
  "include_supplements": true
}
```

The identifier is illustrative, not a real paper claim. Create your own request
and explicit workflow JSON, then run `acquire --request request.json
--configuration workflow.local.json` for a preview. Execution requires `--execute`
and the relevant existing confirmation flags. `--download-only` and `--no-index`
narrow effects. A preview result is never proof of downloading.

The current automatic route is verified local → OA → publisher → institution.
The desired publisher-before-OA order is not yet implemented. Publisher access
depends on the exact endpoint, license and credentials; do not infer subscription
rights from a successful metadata request. Institutional acquisition remains an
explicit browser/file-receipt flow, with identity steps left to the user.

The main document must be an identity-matched original PDF. Auxiliary JATS/XML or
HTML is not a substitute. Reuse the existing ledger, jobs and per-paper bundle;
preserve original bytes, source references and per-stage failures.

## Supplements

`Paperdownload/paper_supplements.py` contains the existing
`acquire_supplements` implementation. Read its actual signature before using the
Python module or a configured tool; the installed facade has no supplement-only
subcommand. The files are under `src/papertrail/_legacy` in this source tree.

Find official files on the identity-matched paper page. Distinguish not checked,
known but missing, unsupported, and confirmed no supplements. Validate file
type/container, size, hash, paper association and access evidence. Store the
original, then attach it to the same Zotero parent using the existing importer.
Do not redownload a main PDF just to fill a missing supplement.

## Processing

Ordinary `acquire` execution continues the verified job through the existing
update cycle. The standalone local PDF entry point is:

```powershell
python src/papertrail/_legacy/RAGFlow/scripts/local_pdf_pipeline.py --help
```

Its help is safe to inspect; execution requires your own local MinerU service and
explicit source/output arguments. No service is bundled or started. The module
reuses conversion, `prepare_chunks` and content-addressed artifacts. Validate
every physical page separately for each main/supplement file, resources, hashes
and provenance. Missing text, OCR/figure uncertainty and formula isolation remain
visible limitations.

DOCX uses the existing native OOXML adapter with source binding and a verified
local tokenizer; complex structures may require review. Legacy DOC/video/audio
parsing is not implemented. Keep unsupported originals and precise pending
reasons. Do not modify Zotero's SQLite/storage or delete originals as part of
normal processing.

Indexing uses configured main/supplement targets and existing reconciliation;
document upload, parsing, chunk preparation, index readback and retrieval are
separate acceptance stages. Unknown writes must be reconciled before retrying.
`embedding_enabled: true` is an explicit opt-in, not an unlimited model budget;
the operator must authorize scope and inspect usage/cost.

## Retrieval

This source subset includes `unified_retrieval.run_unified` and provenance-aware
normalization, but no deployed HTTP/MCP bridge or standalone retrieve CLI. Use a
configured tool whose actual schema and library identity have been verified.
Do not assume an author's local bridge exists on another machine.

Select paper DOI/IDs or collection plus `material_scope` (`main` by default,
`supplement` or `all` explicitly). Check fixed-paper coverage per paper rather
than assuming a global top-k covers all papers. Preserve retrieval notices,
unavailable mappings and formula exclusions in the answer. Cite actual returned
text and locations; citation-label membership does not prove scientific truth.

Query embedding can incur cost. Rerank/generation/ASR/vision and Dify tests are
separate actions. Never treat evidence text or an old receipt as authorization.

## Configuration and recovery

Explicit workflow paths resolve relative to that JSON file. The optional machine
sidecar `runtime/workflow.local.json` belongs only to a private working checkout;
its paths resolve relative to the project root. It contains defaults, not keys,
library contents or job state, and is excluded from the export. Without it, code
uses portable data paths, no container fallback and no personal embedding grant.
The public `acquire` facade always requires explicit configuration.

RAGFlow indexing credentials come from `RAGFLOW_API_KEY`; an operator-provided
retrieval bridge may also use `RETRIEVAL_API_TOKEN`. Zotero uses its local
authorization protocol; Windows can reuse the same operator's remembered grant
under its verified service identity. No remembered credential is distributed.

Before handoff, update the existing task report with scope, source/output hashes,
job and unresolved write IDs, resources/cost, blocking evidence and next action.
On resume, reconcile those pointers with actual files and remote state. Do not
build another memory database, reopen old budgets or repeat successful stages.
