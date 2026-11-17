# Ask HR

Ask HR is an internal HR knowledge demo with:

- a Microsoft Foundry hosted agent;
- Foundry IQ over an Azure AI Search index;
- a Next.js, Tailwind CSS, and shadcn/ui chat application;
- HR documents for Global, India, Finland, and Hong Kong;
- Azure infrastructure managed with Azure Developer CLI and Bicep.

## Infrastructure stage

The current stage provisions the isolated `rg-ask-hr-demo` environment,
including:

- Microsoft Foundry account and project;
- `gpt-4.1-mini`, `gpt-6-astra`, `gpt-6.1-sol`, and `text-embedding-3-large`
  deployments;
- Azure AI Search;
- Document Intelligence;
- Storage, Key Vault, Container Registry, and Container Apps;
- Log Analytics and Application Insights;
- managed identities and RBAC.

Application and ingestion code is added only after infrastructure validation
and deployment succeed.

Search remains on the Basic SKU with one replica and one partition. Semantic
ranker uses Standard usage-based billing after the monthly free allowance.
This is separate from Foundry IQ knowledge-retrieval billing for the configured
Search API. See [semantic-ranker billing](https://learn.microsoft.com/en-us/azure/search/semantic-how-to-enable-disable).

The main generation and judge deployments are each configured for 100,000
tokens per minute (`GlobalStandard` capacity 100). Bicep and the agent's
`azure.yaml` keep these allocations aligned; the classifier remains at capacity 10.

The root post-provisioning hook, `scripts\provision_entra.ps1`, validates the
model outputs before making Azure changes. It synchronizes the main, Activity,
judge, and classifier model bindings to `ASK_HR_AGENT_ENV_NAME` (default
`ask-hr-dev`) in the nested `ask-hr` project, including existing environments.

The demo Storage account intentionally accepts authenticated connections from
all networks. `infra\modules\storage.bicep` sets `publicNetworkAccess: 'Enabled'`,
`networkAcls.defaultAction: 'Allow'`, and the account tag `SecurityControl: 'Ignore'`
on every deployment. This tag is the supported exclusion in the inherited
MCAPSGov `StorageAccount_PublicNetwork_Modify` policy; without it, policy
remediation can disable public networking again. Keep the exception on this
demo account, not the resource group. Anonymous blob access and Shared Key
access remain disabled; data access requires Microsoft Entra authorization.

After Storage provisioning and role assignments complete,
`infra\modules\storage-lock.bicep` applies the account-scoped
`keep-demo-storage-public` ReadOnly management lock. It blocks changes to account
configuration, including public networking; Entra-authenticated PDF reads and
ingestion uploads remain available. Remove the lock before reprovisioning or
changing Storage configuration or account-scoped RBAC. A successful infrastructure
deployment recreates it after role assignments; restore it manually if deployment
fails. This is not a permanent governance exemption: a principal permitted to
delete locks can remove it.

## Document schema review

Ingestion scans every PDF beneath `data\global`, `data\india`, `data\finland`,
and `data\hongkong`, including nested folders. Use `--data-dir` to select
another root with the same regional layout; no source manifest is needed.
Region comes from the folder, language follows the documented folder/filename
conventions, and titles/topics come from filenames.
Document IDs are deterministic GUIDs derived from relative paths. Citation
artifact versions fingerprint the PDF, filename, and parser configuration.
There are no artificial policy dates, active flags, or original-source URLs.
PDFs and highlighting annotations are still uploaded to ADLS before indexing.

The query planner semantically classifies local regions from the user's
natural-language question. A resolved region retrieves the local policy plus
the Global baseline; an ambiguous request requires clarification before
retrieval. The classifier exposes a status, not a numeric confidence score.

Search keeps semantic reranking enabled and uses the knowledge base's configured
output budgets (12 documents and 12,000 tokens). An explicit reranker-score floor
of 1.5 retains relevant partial matches while excluding weaker matches. This is
a demo-calibrated setting, not an Azure default or a guarantee of answer quality.
The application forwards all returned policy chunks to answer generation. The
model returns the employee answer and the IDs of its supporting passages. Each
request's structured response schema restricts IDs to the passages supplied to
generation; application code also validates the final selection and attaches
only the selected sources as citations, without a separate citation-count cap.
Only answer text is streamed to the employee, not the internal source metadata.
If no applicable policy information is available, Ask HR explicitly states that
the available information does not cover the request and directs the employee
to HR, without inventing approval or prohibition and without attaching
unrelated citations. Empty retrieval results take this deterministic path without
an additional generation call.

The main agent uses GPT-6 Astra with high reasoning; classification remains a
separate lightweight call. GPT-6.1 Sol remains the evaluation judge and the
Activity POC model, with separate environment bindings so a main-agent model
change does not silently switch either. The answer-generation prompt requires
source-faithful quantities, units, and inclusive/exclusive limits when translating
or summarizing.
It preserves material policy conditions and answers the employee's question
directly, without routine fictional-demo/legal disclaimers or document-metadata
preambles. Company-specific entitlements are briefly attributed to company
policy within the answer, not presented as universal statutory rights.
Questions about a document's status, authority, scope, dates, or
precedence are still answered truthfully from the evidence.
Missing local passages do not establish that no local policy exists: Global
baselines remain explicitly labeled and are not confirmed local entitlements.

Generate and validate the proposed Search schema without analyzing PDFs or
creating an index:

```powershell
uv run --locked python .\scripts\index_hr_documents.py --schema-only
```

Document-only regression cases derived from the SharePoint open-topics
workbook are stored in `tests\document-retrieval-cases.json`.

The production ingestion pipeline uses Document Intelligence's native Markdown
table HTML and section-aware, token-bounded chunking. It preserves table
relationships, physical-page provenance, and extracted content from every page
and section, including headers and footers. DI section hierarchy supplies heading
context; adjacent headings, tables, and text share a chunk when they fit the
enriched token budget. Relevance is handled by native hybrid search
and semantic ranking, not ingestion-time content exclusions. It embeds normalized
content while retaining source text for citations. Embedding and index operations
use the OpenAI and Azure Search SDKs.
Each run compares PDF file hashes with the local
`output\ingestion\ingestion-state.json` file. Only new, changed, or unsuccessfully
processed PDFs are processed. A completed run with unchanged files and settings
makes no Azure calls.
Each changed PDF is finished before the next starts: extract and chunk its text,
generate embeddings, upload its artifacts, update its Search records, and save
its successful file record. Search updates read only that PDF's existing chunk
IDs, upload new chunks, and delete obsolete ones after successful SDK results.
Known deleted PDFs lose their Search records. File hashes are saved after each
successful PDF update; no chunk-ID inventory is saved locally. Ingestion does not
scan the whole index, poll for visibility, or run retrieval checks.

Configure service endpoints and a dedicated index as described in
[the production ingestion and evaluation guide](SHARING.md), then run using
the locked project dependencies:

```powershell
uv run --locked python .\scripts\index_hr_documents.py
```

Schema changes update the index in place when Azure supports them. Adding fields
backfills documents; semantic-ranking and `retrievable` changes do not reprocess
unchanged PDFs. Removing fields or changing immutable attributes automatically
rebuilds the dedicated demo index and processes all PDFs using the retained DI
cache. `--recreate-index` remains a manual override.
Do not use this destructive rebuild policy on a serving production index.
Azure blocks index deletion while a knowledge source references it; back up,
remove, and restore the dependent knowledge resources separately when rebuilding.
Existing source and annotation versions are not overwritten.
See the guide for incremental updates, artifact versions, and manual Foundry
evaluation. No packaging or cloud evaluation is performed automatically.

Before using GUID-based ingestion with an existing web deployment, deploy the
citation-ID validator update; the previous validator accepts only `HR-` IDs.
Existing PDF/annotation artifacts and legacy citation IDs remain readable.
The live evaluation cases now expect GUIDs and check returned answers and
citations, not internal classifier decisions or retrieval calls. Structured
response items are preserved for Foundry scoring; no custom response diagnostics
or Application Insights query is required. Hosted evaluation uses Foundry's
native rubric evaluator with application-defined HR dimensions and citation
checks for every case, plus Response Completeness for the
13 policy-answer cases. The calibrated rubric's policy scope/evidence dimension
replaces generic Groundedness as the factual-support check in the active suite.
The generic judge is retired from this profile, not silently marked passed.
Response Completeness and citation checks remain acceptance
requirements. PDF-backed references are in
`tests\agent-quality-references.json` (`--references`); source hashes and
question/reference coverage are checked before invocation. The four
clarification/no-policy cases still require rubric and citation acceptance.
This is a limited demo contract, not evaluation of every scenario-specific
assertion: active cases reject unscored free-text requirements and
primary/supporting evidence-role declarations. References are
assistant-source-checked and need customer/HR review
before acceptance use. The rubric replaces the custom HR prompt grader and the
generic grounding check: request handling, policy scope/evidence, and communication quality
have weights 10/5/2, with a default native threshold of 0.75 on the 0-1 scale.
Application acceptance additionally requires **every dimension to score at
least 3/5**; Foundry's raw score and pass flag remain unchanged. Old 1-5 HR scores
and generic-metric results are retained, not relabeled as the new assessment.
The earlier 16/17 result was a calibration-set, session-composed result, not a
fresh run of this contract; see [the evaluation limitations](SHARING.md#evaluation-limitations).
That section retains historical runs and limitations; earlier profiles are not
the current v4 baseline.
Use
`--cases tests\agent-quality-cases-legacy.json` with `--replay-cases` for historical
pre-migration captures; those original case bytes are retained unchanged.

The manually run `scripts\benchmark_ask_hr_pipeline.py` combines pipeline
latency with Foundry SDK evaluation of the same generated answers, using all
17 agent-quality cases. See the guide's combined performance and agent
evaluation section for isolated before/after testing.

For evaluator sanity checks without generating new answers, use
`scripts\run_foundry_evaluation.py --replay-cases` with the reviewed references in
`tests\agent-evaluator-reference-cases-v2.json`. `--prepare-replay` validates and saves
the inputs locally before human review; omit it only after reference approval.
The replay preserves source reports, uses actual captured policy context and
hash-verified generation instructions, and reports judge false passes/failures.
See the guide's saved-answer replay section for the documented evaluator inputs
and limitations.

The same local-first, Global-fallback rule applies independently to every
country and topic. Version 2 corrects the reference that required unused Global
leave terms when both countries already had local leave policies. It preserves
all local facts and the other cases; the original reference file remains
unchanged for historical replay.

Run document retrieval regressions:

```powershell
uv run --locked python .\scripts\validate_document_retrieval.py
```

## Python package feed

Python dependencies must resolve through Microsoft Central Feed Services:

```text
https://packagefeedproxy.microsoft.io/pypi/simple
```

The repository-level `uv.toml` applies this feed to `uv lock`, `uv sync`,
`uv run`, and `uv pip` commands and enables system certificate trust. Do not
configure public PyPI as a fallback index.

For any direct pip invocation, set:

```powershell
$env:PIP_INDEX_URL = "https://packagefeedproxy.microsoft.io/pypi/simple"
```

## Azure Developer CLI

Foundry-related azd commands must set this value inline:

```powershell
$env:AZURE_DEV_USER_AGENT = "microsoft_foundry_skill"
```

Do not persist that value in project configuration.

The hosted-code bundle uses `ask-hr\src\ask-hr\.agentignore` to exclude local
credentials, virtual environments, evaluation state, and test tooling while
retaining the bundled Linux dependencies in `packages`. The legacy
`.azdignore` is not the agent-specific packaging filter.

From the `ask-hr` directory, deploy the Responses service with
`azd deploy ask-hr --no-prompt`. This normal packaging path supplies the
`code-zip` artifact metadata required by the hosted-agent extension.
