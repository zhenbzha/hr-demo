# Ask HR hosted agent

The hosted agent exposes only the Microsoft Foundry Responses 2.0.0 protocol.
Every request must contain:

```http
x-client-search-authorization: <delegated Azure AI Search user token>
x-ms-user-identity: <trusted represented-user object ID>
```

Foundry resolves `x-ms-user-identity` into trusted platform user context. The
handler fails closed unless both that context and the delegated Search token
are present.

For each current question, the handler:

1. reevaluates the applicable HR region;
2. retrieves local-region and Global evidence;
3. authenticates Search with the hosted-agent managed identity;
4. forwards the user token only as `x-ms-query-source-authorization`;
5. generates from the current authorized evidence;
6. returns Responses text, response ID, URL annotations, and safe citation
   metadata for the web client.

Previous user questions can clarify a follow-up, but previous assistant answers
are never evidence. There is no app-only retrieval fallback.

The current Search index has no permission metadata, so valid users still see
the same corpus. The token-forwarding contract is ready for the later native
ACL/index migration.

Ask HR is a Microsoft Foundry hosted agent that answers regional HR policy
questions for India, Finland, and Hong Kong, using Global policy as a baseline.

## Behavior

- Identifies supported employment regions from natural-language questions.
- Asks for clarification when the region is ambiguous.
- Retrieves local and Global policy evidence separately.
- Gives local policy precedence when local and Global guidance overlap.
- Answers in the user's language.
- Emits document and page references for the web application's source cards.
- Rejects unsupported regions and does not invent missing policy details.

## Project structure

```text
azure.yaml
scripts/
  grant_agent_search_role.ps1
src/ask-hr/
  main.py
  pyproject.toml
  uv.lock
  eval.yaml
```

## Required environment values

The active `azd` environment supplies:

- `AZURE_AI_CLASSIFIER_MODEL_DEPLOYMENT_NAME`
- `AZURE_AI_MODEL_DEPLOYMENT_NAME`
- `AZURE_AI_PROJECT_ENDPOINT`
- `AZURE_SEARCH_ENDPOINT`
- `AZURE_SEARCH_KNOWLEDGE_BASE`
- `AZURE_SEARCH_KNOWLEDGE_SOURCE`

The hosted service also receives `ASK_HR_SUPPORTED_REGIONS_JSON`. Add a new
region by updating that configuration and ingesting documents with the matching
Search `region` value; the classifier code does not require a new branch.

Copy `src/ask-hr/.env.example` to `.env` only for local development. Never
commit `.env`.

## Deploy

Run from this directory:

```powershell
$env:AZURE_DEV_USER_AGENT = 'microsoft_foundry_skill'
azd deploy --no-prompt
```

The post-deploy hook grants the hosted-agent identity read access to the Search
index. Each successful deployment creates an immutable agent version.

## Validate

Use the repository-level Foundry evaluation runner against the deployed Responses
endpoint. From the repository root, configure the environment variables described
in the [evaluation guide](../SHARING.md#manual-foundry-evaluation), then run:

```powershell
uv run --locked --extra evaluation python scripts\run_foundry_evaluation.py
```

All 18 cases receive the application-defined HR rubric, a dimension-score floor,
and deterministic citation checks. The 14 policy-answer cases also receive Response
Completeness checks against PDF-derived references. The v6 rubric grades
employee-facing answer text against the PDF facts and expected answers derived
from them. Evaluation-only policy overrides are rejected. It does not grade
internal citation messages as prose or claim
source-only Groundedness. Historical reports remain unchanged.
These are limited
demo checks, not verification of every scenario-specific requirement.
GPT-5.4 is the default judge, separate from the main and Activity agent models;
GPT-5.5 is a comparison judge. The rubric uses the question's language and accepts
honest acknowledgment of details absent from the policy. Its shared
assistant-authored answer key requires HR review before acceptance use.
See [evaluation limitations](../SHARING.md#evaluation-limitations) for scope
and known failures.

Review the saved report; add `--require-behavior-pass` when failed demo checks
must produce a nonzero exit code.
