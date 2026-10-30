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

## Document schema review

Each document has a stable `documentId` plus normalized `region`, `language`,
`policyTopic`, version, effective/review dates, source URI, and SHA-256 hash.
Only manifest entries with `isActive: true` are indexed.

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
It also requires coverage of applicable policy conditions and document-wide
qualifications, without narrowing legal or contractual precedence statements.
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
Current chunks are uploaded and verified before stale chunks are removed.

Configure service endpoints and a dedicated index as described in
[the production ingestion and evaluation guide](SHARING.md), then run using
the locked project dependencies:

```powershell
uv run --locked python .\scripts\index_hr_documents.py
```

Do not use `--recreate-index` on a serving production index; it explicitly
deletes the index. Existing source and annotation versions are not overwritten.
See the guide for cache migration, artifact versions, and manual Foundry
evaluation. No packaging or cloud evaluation is performed automatically.

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
