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
- `gpt-4.1-mini`, `gpt-6.1-sol`, and `text-embedding-3-large`
  deployments;
- Azure AI Search;
- Document Intelligence;
- Storage, Key Vault, Container Registry, and Container Apps;
- Log Analytics and Application Insights;
- managed identities and RBAC.

Application and ingestion code is added only after infrastructure validation
and deployment succeed.

## Document schema review

Each document has a stable `documentId` plus normalized `region`, `language`,
`policyTopic`, version, effective/review dates, source URI, and SHA-256 hash.
Only manifest entries with `isActive: true` are indexed.

The query planner semantically classifies local regions from the user's
natural-language question. A high-confidence classification retrieves the
local region plus the Global baseline; low confidence requires clarification
before retrieval.

Generate and validate the proposed Search schema without analyzing PDFs or
creating an index:

```powershell
python .\scripts\index_hr_documents.py --schema-only
```

Document-only regression cases derived from the SharePoint open-topics
workbook are stored in `tests\document-retrieval-cases.json`.

The ingestion pipeline follows the maintained
`Azure-Samples/azure-search-openai-demo` Document Intelligence parser and
token-aware splitter pattern. It reads local PDF bytes directly, preserves
tables in reading order, removes document-control boilerplate, embeds chunks,
and pushes them to Azure AI Search.

Run ingestion through the isolated Microsoft CFS environment:

```powershell
uv run --with azure-ai-documentintelligence --with azure-identity --with tiktoken `
  python .\scripts\index_hr_documents.py --recreate-index
```

Run document retrieval regressions:

```powershell
python .\scripts\validate_document_retrieval.py
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
