# Production ingestion and manual Foundry evaluation

These are the application's production scripts, not a separate simplified
implementation. No distribution package has been created.

Ingestion and evaluation are independent, manually started operations. They
assume one operator. There is no concurrent-run orchestration, scheduled
evaluation, prompt-optimization loop, or deployment step.

## Source layout

- `scripts\index_hr_documents.py`: ingestion configuration and orchestration.
- `scripts\json_output.py`: shared JSON output and atomic file replacement.
- `scripts\ingestion\sources.py`: folder discovery, GUIDs, hashes, and artifact paths.
- `scripts\ingestion\state.py`: local per-PDF hashes and ingestion progress.
- `scripts\ingestion\document_parser.py`: Document Intelligence layout extraction,
  native table HTML, all extracted pages/sections, and original PDF coordinates.
- `scripts\ingestion\page.py`: page, block, line, and chunk data structures.
- `scripts\ingestion\textsplitter.py`: section-aware sentence and table chunking.
- `scripts\ingestion\semantic.py`: normalized embedding text and Search documents.
- `scripts\ingestion\analysis_cache.py`: saved PDF analysis used automatically
  when retrying processing.
- `scripts\ingestion\search_schema.py`: Search fields and vector configuration.
- `scripts\ingestion\search_index.py`: OpenAI/Search SDK calls, per-PDF updates,
  and acknowledgement validation.
- `scripts\ingestion\storage.py`: versioned PDFs and citation annotations.
- `scripts\ingestion\azure_http.py`: shared API constants and the REST adapter
  used by the knowledge-configuration, retrieval-validation, and hosted-agent
  evaluation scripts. Production embeddings and index operations use SDKs.
- `scripts\run_foundry_evaluation.py`: the manually initiated evaluation workflow.
- `scripts\evaluation\agent.py`: hosted-agent response and citation parsing.
- `scripts\evaluation\cases.py`: case validation and deterministic checks.
- `scripts\evaluation\references.py`: PDF-hash verification, independent reference
  answers, and policy-answer versus clarification/no-policy case coverage.
- `scripts\evaluation\foundry.py`: Foundry SDK scoring and result interpretation.
- `scripts\evaluation\runner.py`: shared dataset submission, polling, and result
  persistence for hosted-agent evaluation and the performance benchmark.
- `scripts\check_evaluation_judge.py`: optional frozen-rule check of the grader
  using authored good/bad answers in `tests\agent-quality-judge-checks.json`;
  this does not invoke an agent.

For a future two-PDF handoff, the runtime source is the two entry points,
`scripts\json_output.py`, both module directories including their `__init__.py`
files, `pyproject.toml`, `uv.lock`, `uv.toml`, and
`scripts\evaluation.env.example`. Inputs are one Global PDF and one local-policy
PDF in their regional folders, plus evaluation cases and a PDF-backed reference
file appropriate to those policies (`tests\agent-quality-references.json` is
the full-corpus example). No ingestion source manifest is required.
Keep this guide and the offline regression tests with the source.
Include the optional judge-check script and its manifest when demonstrating how
to check the evaluator itself. Its source references require the specific Global
handbook and Hong Kong leave-policy PDFs named in that manifest.

The existing production inputs contain 13 PDFs and 18 agent-quality
cases. Reducing a future handoff must not reduce production coverage. The web
app, hosted-agent implementation, benchmark scripts, deployment state, caches,
and generated results are not prerequisites for these two scripts. Evaluation
does require an already deployed, compatible Ask HR Responses endpoint.

## Dependencies and access

Use Python 3.12 or later, `uv`, and Azure CLI. Restore the locked dependencies
into the project-local `.venv`:

```powershell
uv sync --locked --extra evaluation
az login
```

The evaluation extra requires stable `azure-ai-projects>=2.11.0`; the lockfile
selects 2.11.0, matching the current manual-rubric sample's minimum. This is the
root evaluation environment, not an upgrade of the separate agent runtime.

On Microsoft-managed devices, use the approved package feed:

```powershell
$env:UV_INDEX_URL = 'https://packagefeedproxy.microsoft.io/pypi/simple'
```

Use least-privilege access for the operations being performed: Document
Intelligence analysis, Azure OpenAI inference, Search index administration and
document access, ADLS Gen2 data access, and Foundry evaluation. The Search
service's managed identity also needs inference permission for query-time
vectorization. No account keys or persisted access tokens are required.

The entry points own credentials and SDK client lifetimes through context
managers. DI, embedding, and storage clients are reused across the per-PDF loop.
Processing helpers receive configured clients and do not close them; the same
Search client is reused across file-scoped key lookups. SDK acknowledgement
results determine whether updates succeeded; there is no visibility polling.
Buffered writes use a configured SDK constructor to create a fresh sender for
each upload/delete operation, keeping acknowledgement state separate. There are
no singleton clients or dependency-injection framework.

## Ingestion configuration and source integrity

Set these environment variables, or use their matching CLI options:

- `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT`
- `AZURE_OPENAI_ENDPOINT`
- `AZURE_OPENAI_EMBEDDING_DEPLOYMENT`
- `AZURE_SEARCH_ENDPOINT`
- `AZURE_SEARCH_INDEX_NAME` (an explicitly selected, dedicated index)
- `AZURE_STORAGE_DFS_ENDPOINT`
- `AZURE_STORAGE_FILE_SYSTEM`

`AZURE_OPENAI_EMBEDDING_DIMENSIONS` is optional; the default is 1,024.
The deployment must serve `text-embedding-3-large`. Document embeddings and the
Search query-time vectorizer must use the same deployment and dimensions.

`--data-dir` defaults to the repository's `data` directory. Every PDF under its
`global`, `india`, `finland`, and `hongkong` subfolders is checked, including
nested folders and `.PDF` extensions. Only new or changed PDFs are processed;
unchanged PDFs recorded as successfully processed are skipped. Other file types are ignored. Remove
superseded PDFs from this directory rather than marking them inactive.
An empty directory, unsupported regional location, unreadable directory/PDF,
symbolic link, or junction stops ingestion before index changes. Directory links
are rejected rather than silently skipped, which could otherwise delete the
omitted sources' existing Search chunks during synchronization.

The existing corpus conventions supply language defaults: English for Global
and India, Finnish for Finland, and Traditional Chinese for Hong Kong.
A filename suffix `_en`, `_fi`, `_zh-HK`, or `_zh-Hant` overrides that default.
These are input conventions, not automatic language detection. Document titles
and topic text come from filenames; no policy dates or source URLs are invented.
`source_uri` identifies the relative ADLS PDF path.

Source IDs are standard UUID5 strings generated from
`"ask-hr:" + relative_path.as_posix().casefold()` using the standard URL namespace.
The same regional path keeps its ID across machines and content edits.
Renaming or moving a PDF within the data directory changes its ID (except for
letter-case-only changes). Duplicate case-folded identities are rejected.

SHA-256 is calculated from each discovered PDF and checked again around analysis
and before upload. This detects mid-run changes and prevents reuse of cached
analysis for different bytes; it is not an approval signature or governance
check. There is no manually maintained hash list or published inventory.

All extracted pages and sections are retained, including first pages,
acknowledgements, revision history, and references. Hybrid search and semantic
ranking determine relevance; ingestion does not discard content based on its
heading or page number. There is no exclusion configuration or review workflow.
The former `--manifest` option is removed; an old JSON manifest does not select,
exclude, or supply metadata for any source.

DI page-header, page-footer, page-number, and page-break comments remain in
`chunk_text`. Their contents also remain in `semantic_text`, so HTML comment
handling does not hide potentially meaningful header/footer text from embeddings
or ranking. Original OCR lines remain in citation annotations.

Generate a local schema without Azure calls:

```powershell
uv run --locked python scripts\index_hr_documents.py --schema-only
```

This still validates local PDFs and requires the OpenAI endpoint/deployment
configuration so the schema contains the intended query vectorizer.

Start ingestion manually:

```powershell
uv run --locked python scripts\index_hr_documents.py --data-dir .\data
```

Configuration, local file discovery, and hash comparison happen first. If files
and settings are unchanged, no Azure clients are created. Otherwise, read the
index definition, choose the schema action, save unfinished PDF IDs, and apply
the index update. Applied settings are saved only after Azure accepts the update.

Then finish **one changed PDF at a time**:

1. Extract its text using the DI cache where available, and build its chunks.
2. Generate embeddings for those chunks.
3. Upload that PDF and its citation data to ADLS.
4. Query only its existing Search chunk IDs using `document_id`.
5. Upload its new chunks and require successful SDK results before deleting
   existing IDs absent from the replacement set.
6. Save its successful file hash, version, and chunk count; clear its unfinished
   marker before starting the next PDF.

After changed PDFs finish, delete the Search records for known removed PDFs and
save those removals. Finally, write `ingestion-summary.json`. There are no separate
whole-corpus extraction, embedding, or artifact-upload passes. If a later PDF
fails, earlier completed PDFs remain saved and are skipped on retry.

If every PDF is unchanged and the previous run completed, the run makes no Azure
calls. It reads local files and writes the local schema/summary only. Changed runs
query only the affected PDFs, selecting chunk IDs without text or vectors.
The summary reports locally recorded counts, not a live index audit.
Deleting a known PDF only removes its Search records; the remaining PDFs are not
processed again.

`--skip-adls-upload` is only for workflows where the matching citation artifacts
already exist, or source links are intentionally outside the exercise.
Ingestion no longer runs a sample search or whole-index verification, so
`--skip-validation` has been removed. Run `scripts\validate_document_retrieval.py`
or the evaluation scripts separately when retrieval testing is needed.

## Extraction, chunks, and embeddings

Document Intelligence supplies Markdown with native HTML tables, structured
sections, paragraph/cell roles, spans, and page coordinates. The parser reads table HTML directly
from the returned content spans; it does not reconstruct cells or wrap tables
in invented figures. Captions, merged-cell attributes, and escaped text are
retained. Structured cell roles identify column headers for large-table splitting,
including when the native HTML does not contain `scope` attributes.
Python-compatible `unicodeCodePoint` offsets are requested explicitly. The
parser preserves every page and retains every contributing physical page number
for cross-page chunks and tables.
Section references and paragraph roles provide inherited heading context.
Adjacent headings, text, and tables are packed together when their combined
content and heading metadata fit the token budget; a heading or table is not
automatically a separate search document. All original content is retained.

`chunk_text` preserves layout-aware policy content for evidence and citations.
`semantic_text` flattens supported layout markup, captions, and table cells,
retains comment contents, and adds document title, section heading, and policy
topic. Both the embedding input
and semantic-ranking configuration use this normalized text. Policy values,
including deadlines, come from extracted source text; none are injected by the
normalization code. Formatting-only whitespace between HTML cells is ignored so
native multiline HTML produces the same `label | value` relationships as compact
HTML. Spaces within cells and captions are preserved.

The target is 500 tokens for the complete enriched embedding input, with up to
10% sentence overlap within the same contiguous section. These are starting
settings, not a claim that one chunk size is optimal for every policy.
Small tables retain the service's HTML unchanged. Large tables split at connected
row-group boundaries and repeat column headers and captions. Their fragments use
direct table rows rather than leaving unmatched table-section wrappers.
Rowspans remain intact. Headers spanning into body rows require keeping that
table whole. Indivisible table content may exceed the target but must stay
within the 8,191-token embedding limit; otherwise ingestion fails with an
actionable error instead of truncating policy text.

Embeddings must have the expected model identity, dimensions, unique input
indices, and finite, nonzero vector values. The OpenAI SDK handles embedding
requests and retries, using refreshable Entra tokens. Search uses HNSW with cosine
similarity.
The knowledge source includes `chunk_vector` in `searchFields` alongside the
text fields, so native knowledge-base retrieval can also use the configured
query-time vectorizer rather than restricting retrieval to text fields.

## Incremental updates and artifact versions

The index belongs exclusively to the selected data directory. The script maintains
`output\ingestion\ingestion-state.json` (or beneath `--output-dir`) automatically.
Each PDF has one local record containing its last successful SHA-256 file hash,
PDF/annotation version, and chunk count. Chunk IDs are not saved locally.
File hashes are not stored on Search chunks or reconstructed by reading the index.
`SavedFileRecords` is just the loaded contents of this JSON file; `FileRecord`
describes one successfully processed PDF.

The state also records the processing settings and Search/storage destinations.
Settings are compared separately from the PDF's content hash. New or changed PDFs
are selected using these local records before creating any Azure clients.
There is no manually maintained source inventory.

Changes to parser settings or the upload destination also cause processing.
Added index fields trigger document backfill without deleting the index.
Semantic-ranking and `retrievable` changes update the schema without reprocessing
unchanged PDFs. When changing chunking or indexed document content in code,
increment `INGESTION_VERSION` in `scripts\index_hr_documents.py`.

The first run without a local state file processes all current PDFs. Missing,
invalid, or incompatible state is rebuilt from those files. Moving the data directory
does not change file identity; moving `--output-dir` without its state file starts
a new processing history. Keep one output directory per ingestion target.

Before any Search writes, including index deletion, the script atomically saves
`pending_document_ids`.
This is a list of unfinished PDF IDs, not a journal of chunk operations.
Previous successful file records remain available for ordinary updates. A new
or rebuilt index clears those successful records and marks the entire current
corpus for processing; unfinished/removed IDs remain available for retry cleanup.
After a PDF's upload and
cleanup succeed, its file record is saved and its ID is removed from that list.
If another PDF fails later, completed PDFs do not need to be processed again.
Pending IDs also identify PDFs reverted or removed after a failed attempt.
State and DI analysis use the same atomic JSON-writing helper. Processing-setting
changes retain known file IDs so deleted PDFs can still be cleaned up.

Local state assumes one operator and no out-of-band index modifications.
Preserve or back up `ingestion-state.json`: without it, the script cannot identify
PDFs removed since the previous run. Reprocessing the current folder does not
discover or remove unknown/legacy PDF IDs in the index. Restore the matching state
to recover deleted-file tracking, or rebuild into a new dedicated index.
For a disposable index only, `--recreate-index` rebuilds it from the current PDFs.
The DI cache can stay. No full-index recovery scan is performed automatically.

Changed records are uploaded and acknowledged before old records are deleted.
Unchanged records are kept without being uploaded again. A failed upload does not
clear the previous index. Every per-document acknowledgement is checked, including
partial failures. The standard-library batch iterator retains the 100-document
request bound. The Azure Search SDK handles request retries, oversized-request
splitting, and per-action retries. Its buffered sender runs without background
auto-flushing. Application checks still require complete
acknowledgements and final success for every key. The only document reads are
filtered, paginated key lookups for affected PDFs before their writes. Unchanged
and unrelated PDFs are not queried. No read-back verification, exact-coverage
comparison, or sample query runs after an update.

Search updates are **not transactional**: readers can briefly see old and new
generations together, and successful writes can take time to appear in queries.
Allow earlier writes to become visible before evaluating or retrying an interrupted
run; cleanup can only find keys visible to its per-PDF query.
Atomic cutover would require a separately approved
side-by-side index/alias design; this script does not change that architecture.
Index updates follow Azure's schema rules rather than silently retaining old
field definitions:

| Change | Index action |
| --- | --- |
| Missing index | Create and ingest all current PDFs |
| Add a field | Update in place and backfill current PDFs |
| Change semantic ranking or `retrievable` | Update in place; unchanged PDFs are skipped |
| Remove a field or change its type | Delete, recreate, and ingest all current PDFs |
| Change immutable attributes such as `searchable`, `filterable`, `sortable`, or `facetable` | Delete, recreate, and ingest all current PDFs |

Fields are compared by name with service defaults normalized. Response metadata
and field ordering do not cause rebuilds. Previously added ingestion-tracking
fields are removed by rebuilding when they are absent from the desired schema.
Authentication, transport, and unrelated validation errors are propagated;
they never trigger a delete-and-retry fallback.

`--recreate-index` remains an explicit override. Automatic rebuilds are limited
to required schema changes, and both forms are intended for this disposable
demo index. Deletion is not atomic with re-creation: a failed rebuild may leave
an empty index, but all PDF IDs remain marked for processing on retry. The DI
cache is retained. No side-by-side index or deployment mechanism is introduced.

Azure blocks index deletion while a Search knowledge source references it.
For such rebuilds, back up the dependent knowledge base/source definitions,
remove them in dependency order, and restore them after the index is recreated.
The ingestion script does not modify those knowledge resources.

Source PDFs use `documents/{documentId}/{version}.pdf`; annotations use
`annotations/{documentId}/{version}.json`. An existing PDF must have identical
bytes. Existing annotation JSON must have identical canonical content, although
whitespace and property ordering may differ. Genuinely different PDF or
annotation content must never overwrite cited artifacts. Versions are derived
automatically from the PDF hash, filename, parser version, DI model/API, and
string-index mode, truncated to 32 hexadecimal characters for the existing
reference routes. PDF edits and parser-version changes therefore produce new
artifact paths. Parser/annotation corrections must increment `PARSER_VERSION`.
Different extraction output under an unchanged fingerprint fails explicitly;
it is not silently allowed to replace a previously cited artifact.

The web app still uses the same ADLS PDF and annotation paths. Its only required
change is accepting plain GUIDs as document IDs while retaining legacy IDs.
Deploy that validator change before indexing GUID-based documents into an index
used by the web app. This repository change does not deploy anything.
Old ADLS artifacts are retained so existing citations continue to work.
The live retrieval and agent-quality cases now use the generated GUIDs.

The script automatically saves PDF analysis under
`output\ingestion\document-analysis` (or beneath `--output-dir`). This allows a
failed run to reuse completed extraction. Saved results are checked against the
PDF and parser settings; missing, outdated, or damaged results are regenerated.
Unchanged, successfully processed PDFs do not need to read these analysis files.
The ingestion options `--reuse-analysis`, `--analysis-dir`, and
`--retain-intermediate-artifacts` have been removed.

Saved analysis contains extracted policy text. Keep the output directory private
and review it before sharing. Existing ADLS files are never overwritten to bypass
an integrity failure.

### Production state storage

The local JSON file suits this single-operator workflow. For production workers
on multiple or ephemeral hosts, keep ingestion state in a shared database.
Store one file record per ingestion target and document ID, with its PDF SHA-256,
processing version, last successful run, and completion status. Track unfinished
work by file ID. A persistent chunk-ID inventory is not required for this
file-scoped cleanup approach.

Use database transactions for state changes and a lease or lock to serialize
writers to each index. Mark work incomplete before Search changes, and record
success after SDK acknowledgements. A database transaction does not
make DI, ADLS, and Search operations one atomic transaction; retries must remain
safe. This script assumes one writer, even though its updates are file-scoped.

Normal updates already operate only on affected PDFs. A production system can run
independent index audits or reconciliation jobs for external changes and lost
state; these are not part of this script's ingestion loop. No database or
infrastructure change is implemented here.

## Manual Foundry evaluation

Set values from `scripts\evaluation.env.example` in the shell. The file is a
template, not an automatically loaded environment file. Required variables:

- `FOUNDRY_PROJECT_ENDPOINT`
- `ASK_HR_EVAL_MODEL_DEPLOYMENT_NAME` (the judge deployment)
- `AGENT_ASK_HR_RESPONSES_ENDPOINT`
- `ASK_HR_AGENT_NAME`
- `ASK_HR_AGENT_VERSION`
- `ASK_HR_EVAL_USER_ID` (the represented user's Entra object ID)

Optional labels are `ASK_HR_KNOWLEDGE_BASE`, `ASK_HR_EVAL_DATASET_NAME`, and
`ASK_HR_EVALUATION_NAME`. An active, unversioned agent endpoint does not prove
that it serves the supplied version label; the report distinguishes this from
a versioned endpoint.

For a machine signed in to multiple Azure subscriptions, set
`AZURE_SUBSCRIPTION_ID` or pass `--subscription`. The runner uses the documented
[`AzureCliCredential(subscription=...)`](https://learn.microsoft.com/en-us/python/api/azure-identity/azure.identity.azureclicredential?view=azure-python)
parameter for collection and scoring without changing shared Azure CLI defaults.
If omitted, the SDK uses the current Azure CLI account.

`--model-deployment` overrides the judge environment setting. Existing callers
can still use `AZURE_AI_MODEL_DEPLOYMENT_NAME` as a fallback, but set the dedicated
judge variable to `ask-hr-gpt-5.4`. GPT-5.4 is a
[documented supported rubric judge](https://learn.microsoft.com/en-us/azure/foundry/concepts/evaluation-evaluators/rubric-evaluators#choose-an-llm-judge-model).
It is separate from GPT-6 Astra generation and the GPT-6.1 Sol Activity POC.
Model support does not guarantee correct judgments; review the latest
[evaluation limitations](#evaluation-limitations) before using acceptance scores.

```powershell
uv run --locked --extra evaluation python scripts\run_foundry_evaluation.py
```

The hosted evaluation uses the **PDF-reference profile**, not an intersection
of generic relevance, intent-resolution, coherence and groundedness scores:

| Scope | Checks |
|---|---|
| All 18 cases | Native HR rubric, application dimension-score floor, and deterministic citation checks |
| 14 policy-answer cases | Response Completeness against PDF-derived references |
| Four cases explicitly expecting zero citations | Fixed rubric's clarification/no-policy checks and zero citations; no invented policy reference answer |

Scope is determined from the test expectations **before grading**, never from
whether an answer retrieved evidence or passed a metric. A missing or incomplete
policy answer cannot escape policy-answer scoring. A clarification/no-policy
case must still pass the rubric, its dimension-score floor, and citation checks.
These are deliberately limited demo checks, not a guarantee of every
scenario-specific requirement or overall HR correctness.
The **v6** rubric's `policy_scope_and_evidence` dimension checks correctness
against the PDF evidence and expected facts derived from those PDFs.
The PDF evidence takes precedence over the answer key. This is not solely a
groundedness measurement. Unsupported claims still fail;
honestly explaining that the policy omits a detail must not be penalized for
failing to invent it. All eighteen cases are scored. Weights, the native threshold
and the dimension floor are unchanged.

Evaluation-only interpretation fields are not supported. The Hong Kong notice
case asks about a week of annual leave and expects a request generally seven days
in advance, based on the unchanged PDF quotation on page 3. It replaces the
exact-two-day question without asserting whether that boundary is inclusive or
exclusive. PDFs, agent instructions and captured evidence remain unchanged.

Generic Groundedness is outside v6 acceptance; the optional judge-check
diagnostic still receives source evidence without the answer key. Passing v6
does not validate the older source-only graders. Their failed results, including
the withdrawn post-hoc label-only comparison, remain unchanged.

`--references` defaults to `tests\agent-quality-references.json`. This file
contains independently PDF-derived expected answers, source PDF hashes,
physical page numbers, quotations and review declarations. The loader
rejects evaluation-only interpretations, changed PDFs, changed questions, and
missing/duplicate references before
invoking the agent. References were assistant-authored and source-checked, not
independently adjudicated by an HR expert. Review the references before using this suite
as a customer acceptance gate. Do not copy model answers into the references
or revise references merely to make a run pass. When sharing a smaller corpus,
curate matching cases, reference answers and source entries.
The loader validates hashes and manifest coverage, not whether every quotation
occurs on its stated page. Those quotes were source-checked for the frozen demo
references; repeat that source review if references change.

The script captures answers and citations, retains the complete evaluation
dataset, and submits inline rows using the official SDK pattern. The uploaded
dataset records all 18 cases; the policy-answer run receives the predeclared
14-case subset. The native rubric receives `policy_query`: evaluation-only JSON
with separate `question`, `policy_context` and `reference_answer` fields.
The source context is unchanged; reference
fields are never represented as evidence supplied to the agent. This JSON envelope
is application-defined use of the documented `query` input, not a separately
documented rubric reference-answer field. This query is
used consistently for local and hosted policy grading. Captured conversations
and tool items remain in the dataset for audit, but the native rubric's response
mapping is **`{{item.response}}`**, the employee-facing answer text. The scorer
rejects missing or inconsistent copies of the question, context or answer key
before submission. Hosted collection does not capture tool definitions and
retains `tool_definitions: []`.
Response Completeness receives the same assistant-authored answer key as
`ground_truth`, plus the unchanged answer text. It measures recall and does not
independently verify wrong extra claims; the rubric is the only semantic
correctness gate in this profile. HR must review the shared references before
acceptance use. This follows
[Microsoft's RAG evaluator guidance](https://learn.microsoft.com/en-us/azure/foundry/concepts/evaluation-evaluators/rag-evaluators)
and the official
[Response Completeness sample](https://github.com/Azure/azure-sdk-for-python/blob/main/sdk/ai/azure-ai-projects/samples/evaluations/agentic_evaluators/sample_response_completeness.py).
The PDF-derived expected answer is intentionally included in v6 correctness grading.
Expected control pass/fail decisions, reasons and fabricated agent instructions
are not included.

Responses `function_call` and `function_call_output` items are also retained
in the dataset using the documented
[role/content message schema](https://learn.microsoft.com/en-us/azure/foundry/observability/how-to/evaluation-dataset-schema#messages-with-tool-calls),
as shown in the
[Azure SDK structured-response sample](https://github.com/Azure/azure-sdk-for-python/blob/main/sdk/ai/azure-ai-projects/samples/evaluations/agentic_evaluators/sample_intent_resolution.py).
Their actual names, arguments, call IDs, results, and order are preserved; raw
function items without a `role` are not valid uploaded evaluation messages.
Grounding context preserves the cited passages' document IDs, titles, regions,
and page boundaries. Hosted responses expose cited passages, not necessarily
every passage supplied to generation.

The following adaptations have specific HR/application requirements:

| Adaptation | Reason |
|---|---|
| Collect responses with the existing authenticated caller | Ask HR requires represented-user and delegated Search authorization headers. The generic agent-target sample does not supply these headers. |
| Use the HR rubric, with reference-based correctness and completeness checks | Answers are compared with the supplied fictional policies and declared answer key, including local/Global precedence and appropriate clarification or abstention. [Microsoft's agent-evaluation guidance](https://learn.microsoft.com/en-us/azure/foundry/observability/how-to/evaluate-agent) recommends an agent-specific rubric as the primary measure. |
| Check returned citation IDs, regions, and page fields | Verifies required document membership, allowed/forbidden sources, citation counts, required fields and valid page ranges; it does not verify that cited text occurs on those pages. |

The current [agent-target contract](https://learn.microsoft.com/en-us/azure/foundry/observability/how-to/cloud-evaluation-targets)
and SDK target model do not document forwarding Ask HR's custom authorization
headers. Authenticated collection therefore remains; this is not a claim that
every undocumented service option is impossible.

The HR grader is a native `EvaluatorDefinitionType.RUBRIC` evaluator, registered
and version-pinned using the
[official manual rubric sample](https://github.com/Azure/azure-sdk-for-python/blob/main/sdk/ai/azure-ai-projects/samples/evaluations/sample_rubric_evaluator_manual.py).
It replaces the custom prompt-based HR grader. The active v6 profile uses
PDF-derived expected answers rather than claiming source-only Groundedness.
Response Completeness remains a separate evaluator.
The fixed, always-applicable dimensions are:

| Dimension | Relative weight |
|---|---:|
| HR request handling, including correct clarification/no-policy responses | 10 |
| Reference-based policy correctness, including source scope and local/Global precedence | 5 |
| Communication quality, including the employee's language (`communication_quality`) | 2 |

These dimensions, weights, and the 0.75 threshold are application choices, not
Microsoft defaults. Foundry scores each dimension from 1 to 5 and returns the
normalized weighted result: `(weighted mean - 1) / 4`. A dimension scored 1
fails the rubric regardless of the aggregate. This arithmetic follows the
[SDK's `RubricBasedEvaluatorDefinition.pass_threshold` contract](https://learn.microsoft.com/en-us/python/api/azure-ai-projects/azure.ai.projects.models.rubricbasedevaluatordefinition?view=azure-python)
which states that the default 0.5 corresponds to a raw weighted average of 3.0.
The worked examples on the
[Learn rubric page](https://learn.microsoft.com/en-us/azure/foundry/concepts/evaluation-evaluators/rubric-evaluators)
appear to use a different normalization; that discrepancy is unresolved.
The rubric definition and its hash,
registered version, per-dimension scores, weights, and explanations are retained.
The new metric is named `hr_rubric`, on a 0-1 scale; it is **not directly
comparable** to the old holistic `behavioral_adherence` score.

**Application acceptance** additionally requires every dimension to score at
least 3/5. For example, scores `(5, 2, 5)` yield a native score of approximately
0.779 and a native pass, but fail the demo's application assessment. A dimension
graded 2 or lower cannot be offset by stronger dimensions. Scores of 3 can
still pass: `(5, 3, 3)` yields approximately 0.794 and meets both acceptance
rules. This is an application rule, not a Foundry default or a service execution
error; the raw native score and `passed` flag are preserved.
`rubric_assessment.dimension_score_summary` reports
`observed_minimum_score` separately from `required_minimum_score` for each
dimension. For example, an observed minimum of 5 means every scored example
earned 5; it does not change the required floor of 3.

The application-defined `communication_quality` dimension avoids redefining the
SDK's reserved residual `general_quality`. Unexpected dimensions fail validation
rather than silently changing the score.

Deadline answers are judged on meaning, not exact phrasing: preserve the number,
counting unit, inclusive/exclusive boundary, conditions, and target versus
guarantee. Do not invent a starting date or confuse an inquiry timeline with a
complaint-filing window. Verbatim wording is required only for an explicit
quotation request. Company-specific entitlements need a brief company-policy
attribution, not fictional-demo/legal boilerplate or a separate disclaimer.
`tests\agent-quality-judge-checks.json` contains fourteen current
reference-based positive/negative examples. Expected pass/fail decisions
are declared before scoring and kept out of judge inputs; the reviewed answer
key is deliberately supplied. These checks test the evaluator, not the agent,
and are separate from the eighteen-case answer-quality suite. Historical reports
retain their original inputs and counts; do not recalculate them for these smaller
control sets.

The native evaluator grades the employee-facing answer, not the citation
transport or retained tool-call/result messages. Policy grading receives the
same separate question/source/reference query for local and hosted captures.
When any dataset row contains `agent_query`, every row must still contain it;
those recorded conversations are retained unchanged for audit, not presented
as if they contained the evaluation references.
Response Completeness compares answer text with the independent PDF reference.
The native rubric does not consume arbitrary `expected_behavior` assertions:
the fixed dimension descriptions include HR quality checks and rules derived
from these demo cases, including EEM/UK handling, the supported region list,
and faithful deadline meaning. They are not a general-purpose HR rubric.
Active cases therefore reject `expectedBehavior`, `assertions`,
`expectedPrimaryDocumentIds` and `expectedSupportingDocumentIds`.
`expectedDocumentIds` checks membership,
not primary/supporting use. There is no separate per-case semantic assertion
grader: a particular region label or use of a named handbook section is not
individually guaranteed. Separate citation checks and the PDF-reference
evaluators retain their own case-specific inputs. The dataset's
`expected_behavior` field remains capture metadata, not a native rubric input.
The supplied fictional PDFs, not additional interpretations or external
employment law, define the policy facts for this check.
The citation transport is not a Search tool execution record, so the sample's
tool-execution evaluator is not added merely to score that transport.

The default is a **manual diagnostic report**, not an automatic pass gate.
Citation checks return `passed` or `failed`. There are no internal classifier or
retrieval-execution assertions, custom response-diagnostics contract, or
Application Insights dependency. Active cases retain questions and supported
deterministic citation expectations; policy answers come from the separate
reference manifest. No agent response fields, tracing configuration, or deployment
are changed by this evaluation script.

Null, non-finite, missing, duplicate, or inconsistent evaluation scores are
errors even when Foundry marks a run completed. These errors fail the command
and leave a partial report. Ordinary low scores remain visible for manual
review. `--require-behavior-pass` optionally enforces the configured
native rubric threshold, the minimum score of 3 in every dimension, and
rejects failed citation checks and applicable Response Completeness scores.
`--rubric-threshold` defaults to 0.75 (a weighted mean of
4/5, but not the same grader as the historical holistic score);
Response Completeness retains its built-in threshold (3/5).
This gate also applies when rescoring saved responses with `--benchmark-report`;
the completed scores remain in the report even when the gate fails.

Reports include `evaluation_plan`, reference provenance, separate
`expected_behavior` (the retained native-stage name) and `policy_answers` stages,
the independent `rubric_assessment`, and a combined `assessment` listing
failed checks for every case. Native and application decisions are not
interchanged. The `pdf_source_native_demo_v6` profile records
`evaluation_plan.reference_based_correctness` and
`scoring_inputs.contract = pdf_source_answer_text_v1`.
It records the different scope of generic Groundedness in
`evaluation_plan.retired_evaluators`.
It does not reuse an old failed score as a pass: extra/legacy policy metrics
are rejected, so old reports cannot silently become v6 results. It is **not
directly comparable** to v5's added interpretation, v4's source-only rubric,
v3's generic diagnostic, v2's additional Groundedness
gate, or the historical all-generic-metrics intersection. Historical reports
are retained unchanged. Exploratory benchmark runs also use the native HR rubric while
retaining their generic diagnostic metrics. Generic scores must not be presented
as overall HR correctness.
When validating evaluator changes, keep positive, deliberately incorrect and
borderline controls separate from agent results and retain raw judge decisions.
`evaluation_scope` identifies whether answers came from the local pipeline,
hosted endpoint, historical captures, or authored examples. The built-in
18-case suite is labeled a calibration set, not a holdout. A custom case file
is not automatically independent or representative.

Evaluator catalog lookup uses the SDK's paged iterator. Its `limit=20` is a
page-size request, not a 20-evaluator total cap; the SDK follows continuation
links. No custom pagination workaround is needed.

Reports are written under `.foundry\results`. The local JSONL dataset is
temporary unless `--retain-dataset` is supplied. The dataset uploaded to
Foundry remains a remote artifact and follows that project's retention/access
policies; deleting the temporary local file does not delete it from Foundry.
Polling is bounded, with a cancellation request on timeout. There is no
automatic rerun, optimization, or deployment.

### Evaluation limitations

The latest fresh hosted run on 2026-11-17 used the comparison GPT-5.5 judge:
**17/18 passed**, including the week-of-leave question. Citation checks passed
18/18 and completeness passed 14/14. The EEM clarification remained below the
0.75 rubric threshold; the quality gate is not overridden.

The eighteen-case suite and fourteen judge checks are calibration material,
not independent HR validation. Expected answers require HR review, and known
grader limitations must not be hidden by averaging scores or relabeling failures.
The former exact-two-day question and its related synthetic checks are retired;
the replacement does not establish an inclusive/exclusive interpretation.

Historical reports are local and gitignored, not available in a fresh
checkout. Earlier profiles and saved-answer regrades are not evidence of a
fresh run under today's contract. Historical replay is retired; use the current
judge-check workflow below rather than restoring missing legacy captures.

### Frozen judge spot check

`tests\agent-quality-judge-checks.json` records PDF hashes, page quotations,
positive/negative answers and predeclared labels. It pins the existing rubric
hash and dimension floor, so changing grading rules causes validation to fail
before a cloud request. New-question examples and known regressions are
reported separately. The source loader verifies hashes and reference coverage;
page-quote accuracy still requires source review.
The current manifest is labeled `reference_calibration` and pins both the rubric
hash and `pdf_source_answer_text_v1` input contract. All fourteen examples have
been scored before and are now labeled as known regressions, not new questions
or an independent holdout. They include an honest MPF limitation versus an
invented rate and a Global confidential-data rule. The two exact-two-day
planned-leave examples have been removed.
These are assistant-authored checks, not independent HR or statistical validation.

Prepare locally, review the sources and expected decisions, then score into a
different, previously nonexistent output directory:

```powershell
uv run --locked --extra evaluation python scripts\check_evaluation_judge.py `
  --prepare-only --output-dir output\judge-check-prepared

uv run --locked --extra evaluation python scripts\check_evaluation_judge.py `
  --project-endpoint $env:FOUNDRY_PROJECT_ENDPOINT `
  --model-deployment $env:ASK_HR_EVAL_MODEL_DEPLOYMENT_NAME `
  --subscription $env:AZURE_SUBSCRIPTION_ID `
  --compare-groundedness --output-dir output\judge-check-scored
```

The live command registers a rubric version and incurs evaluation model usage;
it does not invoke or deploy an agent. The explicit subscription selects the
Azure CLI credential without changing global CLI defaults. Expected decisions
and their reasons stay out of judge inputs; only PDF-backed expected answers
are included. A deliberately incorrect answer
being rejected counts as agreement. Any native-rubric disagreement fails the
command. Optional generic Groundedness receives no answer key.
It is a source-only diagnostic, not compared against the reference-based
correctness labels and never used to override native results.
Preserve the first run. Revising a rubric after seeing these examples is
recalibration, not another independent holdout check.
For a model comparison, use the same frozen manifest with each deployed model's
`--model-deployment` name and a distinct output directory. Verify identical
`inputs_sha256`, rubric hashes and private expected decisions before comparing
agreement. A higher average score is not evidence that a judge is more accurate.

## Combined performance and agent evaluation

`scripts\benchmark_ask_hr_pipeline.py` normally runs the same Foundry SDK
evaluators on the answers it times. `--capture-only` instead saves unscored
answers for review. Use the agent's existing virtual environment
so classification and generation use its locked runtime dependencies. Use
`--evaluation-python` to run scoring in the root project's evaluation environment
(`uv sync --locked --extra evaluation`) without upgrading the agent SDK. The
agent's older `azure-ai-projects` can run the agent but lacks the evaluator catalog;
a local capability check fails before collecting answers if scoring is unavailable.
Set the
agent's project endpoint, generation/classifier deployment names, Search endpoint,
and web base URL in the shell first.

```powershell
.\ask-hr\src\ask-hr\.venv\Scripts\python.exe scripts\benchmark_ask_hr_pipeline.py `
  --knowledge-base-name <test-kb> --knowledge-source-name <test-ks> `
  --evaluation-python .\.venv\Scripts\python.exe `
  --label "Candidate answer quality and latency" `
  --output output\agent-quality\candidate.json
```

Coherence currently supports
[English-language responses only](https://learn.microsoft.com/en-us/azure/foundry/concepts/evaluation-evaluators/general-purpose-evaluators).
The benchmark therefore scores it in a separate `english_coherence`
stage, selecting only reviewed actual-response language tags of `en` or `en-*`.
Language is not guessed from the question, employee region, or reference answer.
`coherence_applicability` lists eligible and skipped cases. Unsupported or
unknown-language results do not enter Coherence aggregates; no eligible answers
means zero scored cases and null aggregate scores, not a pass.
The hosted PDF-reference profile does not use Coherence.

To include English Coherence, add `--capture-only` to the benchmark command.
Keep an unchanged copy of the captured report. Review each actual answer and
add `response_language` to its entry in `results` (`en`, `fi`, `zh-HK`, or the
appropriate language tag); do not change answers or captured context.
Capture-only leaves `status: incomplete`, `response_collection: completed`,
and `scoring_complete: false`. Then score the reviewed file:

```powershell
uv run --locked --extra evaluation python scripts\run_foundry_evaluation.py `
  --benchmark-report output\agent-quality\candidate.json `
  --project-endpoint $env:FOUNDRY_PROJECT_ENDPOINT `
  --model-deployment $env:ASK_HR_EVAL_MODEL_DEPLOYMENT_NAME
```

Use the same cases, project, judge, threshold and dataset settings as the capture.
Successful scoring marks the report completed without collecting new answers.
Unreviewed language tags can be left absent; Coherence is then explicitly skipped.

The benchmark verifies the actual knowledge-base/source/index binding, runs all
18 hard-requirement cases sequentially, and captures classifier decisions,
retrieval scopes, generated answers, structured citations, and the exact compact
policy context supplied to generation. It reuses the same case checks and
Foundry scoring code as the hosted-endpoint evaluator, rather than relying on
hard-coded answer substring checks. It does not change Search or agent bindings.
New captures also retain the generation and classification instruction text next
to their hashes, the generation reasoning effort, and a response-schema template.
Each generated case records its actual response schema, including the allowed
source IDs, before answer-source selection. Cases that skip generation record no
generation schema.
The unified agent uses GPT-6 Astra with high reasoning to improve coverage of the
supplied policies; keep this setting fixed when testing ingestion or retrieval changes.
`AZURE_AI_MODEL_DEPLOYMENT_NAME` selects generation for both Responses and Activity;
`ASK_HR_EVAL_MODEL_DEPLOYMENT_NAME` independently selects the judge.
Normal benchmark scoring uses the recorded generation
conversation for relevance, groundedness, and intent resolution. It verifies the
instruction hash and agreement between the recorded question, policy context,
and model input before scoring. No ground-truth answer is invented for ordinary
benchmark rows. Captures without a generation turn retain question-only inputs;
missing instructions or model turns are never fabricated.

The agent uses native Search reranking (`resultsProcessing: "rerank"`) with
minimal retrieval reasoning. Minimal reasoning disables LLM query planning,
not semantic reranking. The source searches both text and vector fields.
Requests explicitly set `rerankerThreshold: 1.5` to retain relevant partial
matches while excluding weaker matches. A zero floor was rejected because an
unsupported-policy question received unrelated citations. The nonzero floor is
calibrated for the demo corpus, not an Azure default or a guarantee of answer
quality; recheck positive and no-evidence cases when content or ranking changes.
This retains semantic ordering and does not change the LLM evaluation thresholds.
The knowledge base controls the document and token budgets through
`retrieveDefaults` (12 documents and 12,000 tokens).
Neither the request nor evidence compaction imposes a separate five-chunk cap.
All returned nonempty passages reach generation. The model returns a structured
employee answer and supporting passage IDs. Each request constrains those IDs to
the supplied passages through a string enum in the structured response schema;
JSON parsing and the final source-selection guard both enforce membership.
The application attaches only the selected sources, with no separate citation-count
cap. Unknown IDs, missing sources for a policy answer, and malformed or incomplete output fail
explicitly. The Responses path streams only decoded answer text and validates
the completed result; both adapters and the benchmark use the same source
selection. Source-ID validation proves membership in the supplied evidence, not
semantic support: source-aware quality evaluation remains necessary.
Benchmark rows retain the original scoped Search
responses, including activity and reference scores, alongside the compact model
input. Check that the required policy passage reaches generation; retrieving
the correct document title or passing a separate Search benchmark is not enough.
When a passage spans chunks, check that every required part reaches the model;
requiring the entire passage to appear in a single chunk is not a completeness test.

The report contains classification, retrieval, generation, and total pipeline
latency (median and inclusive-interpolated p95), plus per-case Foundry scores,
judge reasons, run IDs, and portal report URLs. Judge execution is outside the
pipeline timing. Answers failing deterministic checks are still scored and their
latencies retained. A failed collection or incomplete/unscorable Foundry result
leaves an explicitly incomplete report, never a successful latency-only result.
Ordinary quality failures remain a manual report; `--require-behavior-pass`
optionally enforces citation checks and the application `rubric_assessment` as
a failing exit status, including the minimum dimension score of 3/5. Raw
`quality.hr_rubric` statistics still describe Foundry's decision, not application
acceptance. The native `--rubric-threshold`
defaults to 0.75 on the 0-1 scale; `--judge-model-deployment` can select a judge
separately from generation.

After an evaluator execution failure, `run_foundry_evaluation.py --benchmark-report
<path>` can rescore completed captured responses. It verifies and reuses the
registered Foundry dataset version for capture lineage rather than uploading it
again. Scoring consumes the verified capture-derived rows inline, with an input
hash recorded in the report. This prevents an older registered dataset without
the captured conversation from silently replacing the corrected evaluator input.
Use the same project, dataset name, cases, and scoring configuration, and preserve
the failed report before rerunning. This does not repeat agent invocation or
latency collection.

For an ingestion A/B, use two isolated indexes and knowledge bases with identical
agent code, prompts, models, questions, and evaluator configuration. This tests
the full local production decision/retrieval/generation pipeline with live Azure
services, not a replay of saved search results. It does not test hosted HTTP
transport, the web UI, or PDF/highlight rendering. Source hashes and SDK versions
identify the evaluated local code; they are not a claim about a deployed version.
Scoring SDK versions are recorded separately from agent runtime SDK versions.
The JSON report retains policy text and answers for review and is ignored by Git.

## Offline checks

These checks cover ingestion, search normalization, benchmarking, and evaluation
with local fixtures and mocked clients, not live Azure services:

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'scripts')
uv run --locked --extra evaluation python -m unittest discover -s tests -p 'test_*.py'
```

## Microsoft references

- [Document Intelligence spans and layout](https://learn.microsoft.com/azure/ai-services/document-intelligence/concept/analyze-document-response?view=doc-intel-4.0.0)
- [Native Markdown tables and page metadata](https://learn.microsoft.com/azure/ai-services/document-intelligence/concept/markdown-elements?view=doc-intel-4.0.0)
- [Chunking for vector search](https://learn.microsoft.com/azure/search/vector-search-how-to-chunk-documents)
- [Structure-aware chunking with Document Intelligence](https://learn.microsoft.com/azure/search/search-how-to-semantic-chunking)
- [Text and vector fields in agentic retrieval](https://learn.microsoft.com/azure/search/agentic-retrieval-how-to-create-index)
- [Native retrieval and reranking](https://learn.microsoft.com/azure/search/agentic-retrieval-how-to-retrieve)
- [Search index updates and rebuilds](https://learn.microsoft.com/azure/search/search-howto-reindex)
- [Search document loading and partial failures](https://learn.microsoft.com/azure/search/search-how-to-load-search-index)
- [Search SDK buffered sender](https://learn.microsoft.com/python/api/azure-search-documents/azure.search.documents.searchindexingbufferedsender?view=azure-python)
- [Embedding generation with the OpenAI SDK](https://learn.microsoft.com/azure/foundry/openai/how-to/embeddings)
- [Azure OpenAI embedding dimensions](https://learn.microsoft.com/azure/search/cognitive-search-skill-azure-openai-embedding)
- [Custom Foundry evaluators](https://learn.microsoft.com/azure/foundry/concepts/evaluation-evaluators/custom-evaluators)
- [Cloud evaluation results](https://learn.microsoft.com/azure/foundry/observability/how-to/cloud-evaluation-results)
