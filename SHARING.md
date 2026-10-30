# Production ingestion and manual Foundry evaluation

These are the application's production scripts, not a separate simplified
implementation. No distribution package has been created.

Ingestion and evaluation are independent, manually started operations. They
assume one operator. There is no concurrent-run orchestration, scheduled
evaluation, prompt-optimization loop, or deployment step.

## Source layout

- `scripts\index_hr_documents.py`: ingestion configuration and orchestration.
- `scripts\ingestion\manifest.py`: source metadata, paths, and hashes.
- `scripts\ingestion\document_parser.py`: Document Intelligence layout extraction,
  native table HTML, all extracted pages/sections, and original PDF coordinates.
- `scripts\ingestion\page.py`: page, block, line, and chunk data structures.
- `scripts\ingestion\textsplitter.py`: section-aware sentence and table chunking.
- `scripts\ingestion\semantic.py`: normalized embedding text and Search documents.
- `scripts\ingestion\analysis_cache.py`: optional, source- and parser-bound cache.
- `scripts\ingestion\search_schema.py`: Search fields and vector configuration.
- `scripts\ingestion\search_index.py`: OpenAI/Search SDK calls, synchronization,
  acknowledgement validation, and retrieval checks.
- `scripts\ingestion\storage.py`: versioned PDFs, citation annotations, and the
  published manifest.
- `scripts\ingestion\azure_http.py`: shared API constants and the REST adapter
  used by the knowledge-configuration, retrieval-validation, and hosted-agent
  evaluation scripts. Production embeddings and index operations use SDKs.
- `scripts\run_foundry_evaluation.py`: the manually initiated evaluation workflow.
- `scripts\evaluation\agent.py`: hosted-agent response and citation parsing.
- `scripts\evaluation\cases.py`: case validation and deterministic checks.
- `scripts\evaluation\foundry.py`: Foundry SDK scoring and result interpretation.
- `scripts\evaluation\runner.py`: shared dataset submission, polling, and result
  persistence for hosted-agent evaluation and the performance benchmark.

For a future one-PDF handoff, the runtime source is the two entry points, both
module directories including their `__init__.py` files, `pyproject.toml`,
`uv.lock`, and `scripts\evaluation.env.example`. Inputs are one approved PDF, a
manifest containing that PDF, and evaluation cases appropriate to that policy.
Keep this guide and the offline regression tests with the source.

The existing production inputs still contain 13 PDFs and 17 agent-quality
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

On Microsoft-managed devices, use the approved package feed:

```powershell
$env:UV_INDEX_URL = 'https://packagefeedproxy.microsoft.io/pypi/simple'
```

Use least-privilege access for the operations being performed: Document
Intelligence analysis, Azure OpenAI inference, Search index administration and
document access, ADLS Gen2 data access, and Foundry evaluation. The Search
service's managed identity also needs inference permission for query-time
vectorization. No account keys or persisted access tokens are required.

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

The manifest describes each PDF's ID, version, region, language, policy topic,
effective/review dates, source URL, local path, active status, and SHA-256.
`localPath` resolves from `documentRoot`, itself relative to the manifest.
Without `documentRoot`, paths resolve from the manifest's directory.

The hash confirms that the bytes being analyzed and uploaded match the selected
manifest. It also prevents a cached analysis from being reused for a different
PDF. A hash is not a document-approval signature and does not replace access
control or content governance.

All extracted pages and sections are retained, including first pages,
acknowledgements, revision history, and references. Hybrid search and semantic
ranking determine relevance; ingestion does not discard content based on its
heading or page number. There is no exclusion configuration or review workflow.
Legacy manifests containing `ingestion`, `excludePages`, or `excludeSections`
are rejected with instructions to remove those settings, rather than silently
ignoring them.

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
uv run --locked python scripts\index_hr_documents.py
```

The sequence is:

1. Validate configuration, manifest metadata, and source hashes.
2. Analyze PDFs with Document Intelligence `prebuilt-layout`.
3. Build page-aware chunks and normalized semantic text.
4. Generate and validate embeddings.
5. Persist versioned source PDFs and citation annotations in ADLS.
6. Create a new Search index or update compatible existing settings.
7. Upload current chunks, confirm visibility, then remove stale chunks.
8. Confirm exact chunk coverage and run a hybrid retrieval smoke query.
9. Publish the source manifest and write the local completion summary.

`--skip-adls-upload` is only for workflows where the matching citation artifacts
already exist, or source links are intentionally outside the exercise.
`--skip-validation` skips the hybrid query, not exact chunk-key verification.

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
similarity, with 50 vector candidates for the hybrid semantic smoke query.
The knowledge source includes `chunk_vector` in `searchFields` alongside the
text fields, so native knowledge-base retrieval can also use the configured
query-time vectorizer rather than restricting retrieval to text fields.

## Updates, artifact versions, and cache migration

The index belongs exclusively to this manifest. Current chunks are uploaded
and verified before stale keys are deleted. A failed upload does not clear the
previous index. Every per-document acknowledgement is checked, including
partial failures. The standard-library batch iterator retains the 100-document
request bound. The Azure Search SDK handles request retries, oversized-request
splitting, and per-action retries. Its buffered sender runs without background
auto-flushing. Application checks still require complete
acknowledgements and final success for every key; an SDK flush alone is not a
success guarantee.

Search updates are **not transactional**: readers can briefly see old and new
generations together. Atomic cutover would require a separately approved
side-by-side index/alias design; this script does not change that architecture.
Changing the embedding deployment or dimensions requires a new index.
Existing immutable field settings are retained; newly added fields use the
generated schema. An existing index without sortable/filterable chunk keys is
rejected rather than silently recreated.

`--recreate-index` explicitly deletes the index. It is for disposable indexes,
not a serving production index, and is never an automatic fallback.

Source PDFs use `documents/{documentId}/{version}.pdf`; annotations use
`annotations/{documentId}/{version}.json`. An existing PDF must have identical
bytes. Existing annotation JSON must have identical canonical content, although
whitespace and property ordering may differ. Genuinely different PDF or
annotation content requires a new manifest version; cited artifacts are not
overwritten. In particular, a parser correction may require a version bump
even when the PDF itself has not changed.

Extracted local content is retained only with
`--retain-intermediate-artifacts`. Explicit cache reuse uses
`--reuse-analysis --analysis-dir <directory>` and is bound to source ID,
version, hash, parser/cache version, DI model/API, and string-index mode.
Older or mismatched caches fail with instructions to reanalyze. Parser version 5
adds inherited DI section headings to the existing version-4 cache format, which
retains all extracted pages, page comments, native table HTML, and column-header
metadata. Caches made with earlier parsers must be regenerated without
`--reuse-analysis`; version-4 parser caches lack structural heading context,
and still older caches may already have discarded content.
Changed chunk packing changes chunk IDs and requires re-ingestion to update Search.
Previously omitted pages also change citation annotations, requiring a new
manifest document version if different annotations already exist in ADLS.
Existing artifacts are never overwritten to bypass that requirement.
Do not redistribute caches, extracted policy text, or live evaluation output
without reviewing their contents.

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

`--model-deployment` overrides the judge environment setting. Existing callers
can still use `AZURE_AI_MODEL_DEPLOYMENT_NAME` as a fallback, but set the dedicated
judge variable to keep GPT-6.1 Sol fixed when generation uses GPT-6 Astra.

```powershell
uv run --locked --extra evaluation python scripts\run_foundry_evaluation.py
```

The script invokes cases sequentially, collects answers and full structured
citation text, uploads one dataset, then runs the built-in relevance,
groundedness, intent-resolution, and coherence evaluators followed by the
domain-specific expected-behavior evaluator. The custom evaluator uses the
approved preview API and supplied fictional policy, not external employment
law, as its reference.

The default is a **manual diagnostic report**, not an automatic pass gate.
Deterministic checks report `passed`, `failed`, or `unverified`. The current
Responses adapter does not expose internal classifier decisions or retrieval
calls. Those requirements are therefore **unverified**, not inferred from an
answer or an empty citation list. Optional explicit diagnostics are supported,
but this refactor does not add them to or redeploy the hosted agent.

Null, non-finite, missing, duplicate, or inconsistent evaluation scores are
errors even when Foundry marks a run completed. These errors fail the command
and leave a partial report. Ordinary low scores remain visible for manual
review. `--require-behavior-pass` optionally enforces the configured
expected-behavior threshold and rejects failed or unverified hard checks.
This gate also applies when rescoring saved responses with `--benchmark-report`;
the completed scores remain in the report even when the gate fails.

Reports are written under `.foundry\results`. The local JSONL dataset is
temporary unless `--retain-dataset` is supplied. The dataset uploaded to
Foundry remains a remote artifact and follows that project's retention/access
policies; deleting the temporary local file does not delete it from Foundry.
Polling is bounded, with a cancellation request on timeout. There is no
automatic rerun, optimization, or deployment.

### Saved-answer reference replay

`--replay-cases` evaluates the judges using existing answers, not a hosted agent
target. The reference manifest selects captured case IDs and records
question-specific reference answers, source PDF/DI hashes, page quotes, exact
generation instructions and expected decisions for explicitly reviewed metrics.
The original answers, policy context, citations, prompts and prior scores remain
unchanged. It is a known-example sanity set, not an independent holdout or proof
that the agent is ready for production.

Prepare the inputs without Azure requests, review the critical facts and labels,
then record that review in the manifest before scoring:

```powershell
.\.venv\Scripts\python.exe scripts\run_foundry_evaluation.py `
  --replay-cases tests\agent-evaluator-reference-cases-v2.json `
  --prepare-replay `
  --project-endpoint $env:FOUNDRY_PROJECT_ENDPOINT `
  --output-dir ask-hr\src\ask-hr\.foundry
```

This command uses the dedicated judge setting above, which must match the judge
recorded in the saved captures.

Run the same command without `--prepare-replay` after recording the review basis.
`approved` denotes human reference review. When the user supplies the governing
rule and delegates individual labels, use `guidance_confirmed`, record that rule,
and identify `label_author` as `assistant`. This is not independent human
adjudication or HR-expert validation. Pending references are rejected before any
scoring request. The selected capture files and retained DI analyses must be
present; they are not distributed as ordinary repository fixtures.

The replay uses the documented native Foundry contracts:

- Coherence retains question/answer inputs.
- Groundedness retains the actual captured policy context. Relevance,
  groundedness, and intent resolution receive the original system instructions
  and generation input when a generation turn was captured. Message content uses
  Foundry text
  parts (`[{"type": "text", "text": "..."}]`), preserving the exact captured text;
  it is not supplied as bare string content inside a message array.
- Response Completeness (**preview**) receives `ground_truth` and `response`.
- Task Adherence (**preview**) receives that captured generation turn and uses
  a binary score. Zero is a valid failed judgment, not an execution error.
- The existing expected-behavior evaluator is retrieved by its recorded version
  and checked against the unchanged local rubric, not silently rewritten.

The user-facing clarification message is not the classifier's structured model
response. Old captures without that full classifier turn are explicitly excluded
from Task Adherence, not made to look like generation turns. Their other scores
remain visible, and their appropriate-clarification behavior is checked against
reference answers and the HR behavior rubric. No tool traces are fabricated.

Only explicitly labeled metrics participate in false-pass/false-fail comparisons;
an incorrect factual claim does not automatically imply poor coherence or
incompleteness. Reference labels and expected verdicts are never sent to the judge.
Reference answer text is supplied only through the documented reference input,
not inserted into the agent's historical context.

The maintained references follow the same rule independently for each country
and topic: use applicable local policy first; otherwise use Global and label the
fallback for that country. This is not a separate comparison instruction.
Version 2 removes the requirement to restate unused Global leave terms when
both countries have local policies. Every local reference fact, other case,
source quotation and historical sample label is preserved. The original
`tests\agent-evaluator-reference-cases.json` remains unchanged for prior replays.
A retrieval miss does not prove the absence of a country policy. Both historical
Finland benefits answers selected by the manifest omit available local leave
entitlements, so both have negative reference-completeness labels. Their captured
context contains payroll and Global passages but not the Finland leave document.
Groundedness and historical
instruction adherence are therefore retained as separate diagnostics rather than
automatically labeled failures for unseen source material.

Replay inputs are always retained under the selected output directory's
`datasets` folder, with native evaluation IDs, raw output items, selected catalog
versions and reference comparisons under `results`. Scoring failures leave an
incomplete report. A completed replay can still fail the known-answer sanity
check; execution completion is not quality approval. Remote runs and their inline
datasets remain in Foundry. No source reports, Search resources, agent deployment
or production prompt are changed.

Official contracts:
[RAG evaluators](https://learn.microsoft.com/en-us/azure/foundry/concepts/evaluation-evaluators/rag-evaluators),
[native response-completeness sample](https://github.com/Azure/azure-sdk-for-python/blob/main/sdk/ai/azure-ai-projects/samples/evaluations/agentic_evaluators/sample_response_completeness.py),
and [native task-adherence sample](https://github.com/Azure/azure-sdk-for-python/blob/main/sdk/ai/azure-ai-projects/samples/evaluations/agentic_evaluators/sample_task_adherence.py).

## Combined performance and agent evaluation

`scripts\benchmark_ask_hr_pipeline.py` now always runs the same Foundry SDK
evaluators on the answers it times. Use the agent's existing virtual environment
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

The benchmark verifies the actual knowledge-base/source/index binding, runs all
17 hard-requirement cases sequentially, and captures classifier decisions,
retrieval scopes, generated answers, structured citations, and the exact compact
policy context supplied to generation. It reuses the same case checks and
Foundry scoring code as the hosted-endpoint evaluator, rather than relying on
hard-coded answer substring checks. It does not change Search or agent bindings.
New captures also retain the generation and classification instruction text next
to their hashes, the generation reasoning effort, and a response-schema template.
Each generated case records its actual response schema, including the allowed
source IDs, before answer-source selection. Cases that skip generation record no
generation schema.
The main agent uses GPT-6 Astra with high reasoning to improve coverage of the
supplied policies; keep this setting fixed when testing ingestion or retrieval changes.
`AZURE_AI_MODEL_DEPLOYMENT_NAME` selects main-agent generation,
`ASK_HR_EVAL_MODEL_DEPLOYMENT_NAME` selects the judge, and
`AZURE_AI_ACTIVITY_MODEL_DEPLOYMENT_NAME` keeps the Activity POC on GPT-6.1 Sol.
Normal benchmark scoring, like reference replay, uses the recorded generation
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
optionally makes them a failing exit status. The behavior threshold defaults to
4/5; `--judge-model-deployment` can select a judge separately from generation.

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
