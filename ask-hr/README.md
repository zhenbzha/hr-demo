# Ask HR hosted agent

One hosted agent, `ask-hr`, exposes the Microsoft Foundry Responses 2.0.0 and
Activity 2.0.0 protocols from the same `main.py` process. Both adapters share
`ask_hr_pipeline.py`, GPT-6 Astra, the classifier, and the same Search corpus.
The SDK composes both route sets on one server; there is no second agent or
protocol-to-protocol proxy.

This guide describes the authenticated `main` configuration. For the separate
no-login web demo, see the [branch details](../README.md#try-it).

## Authentication and retrieval

Responses requests must contain:

```http
x-client-search-authorization: <delegated Azure AI Search user token>
x-ms-user-identity: <trusted represented-user object ID>
```

Foundry resolves `x-ms-user-identity` into trusted platform user context. The
Responses handler fails closed unless both that context and the delegated Search
token are present. Its streaming, history, and citation contracts are unchanged.

Activity requests use the existing Microsoft 365 Agents SDK sign-in flow and
the `SEARCH` OAuth connection (`ask-hr-search-sso`). The custom authorization
middleware handles Teams token exchange; the handler obtains the delegated
Search token from the authenticated turn rather than Responses client headers.
The Foundry endpoint declares both `Entra` and `BotServiceRbac` authorization
schemes to retain direct Responses access and channel access.

`BotServiceRbac` requires a signed-in channel user with an Entra tenant and the
agent interaction permission. Assign **Foundry Agent Consumer**, or an equivalent
role, at the agent or project scope for each intended caller. The bot's identity
and Search permissions do not grant the user this endpoint permission. See
[Foundry endpoint authorization](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/agent-endpoint-authorization).

For each current question, the shared pipeline:

1. reevaluates the applicable HR region;
2. retrieves local-region and Global evidence;
3. authenticates Search with the hosted-agent managed identity;
4. forwards the user token only as `x-ms-query-source-authorization`;
5. generates from the current authorized evidence;
6. returns the answer and selected policy sources to the protocol adapter.

Responses emits streaming text, a response ID, URL annotations, and safe citation
metadata for the web client. Activity updates the Teams message with the answer
and formatted source links.

Previous user questions can clarify a follow-up, but previous assistant answers
are never evidence. There is no app-only retrieval fallback.

The current Search index has no permission metadata, so valid users still see
the same corpus. The token-forwarding contract is ready for the later native
ACL/index migration.

## Behavior

- Identifies India, Finland, and Hong Kong from natural-language questions.
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
  configure_activity_sso.ps1
  publish_activity_teams.ps1
src/ask-hr/
  main.py
  activity_app.py
  activity_authorization.py
  ask_hr_pipeline.py
  pyproject.toml
  uv.lock
  eval.yaml
```

## Required environment values

The agent uses Python 3.13 or later and its own dependencies under
`src\ask-hr`. Do not substitute the root ingestion/evaluation environment for
the agent environment.

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
azd deploy ask-hr --no-prompt
```

The post-deploy hook grants the hosted-agent identity read access to the Search
index. Each successful deployment creates an immutable agent version.

### Teams setup

After deploying the agent, configure its direct Activity integration:

1. Confirm the selected nested azd environment contains `AZURE_AI_PROJECT_ID`
   and `AGENT_ASK_HR_BOT_NAME` from the deployment. The SSO helper uses
   `AGENT_ASK_HR_ACTIVITY_ENDPOINT` when available, otherwise it reads the
   registered Azure Bot endpoint. The URL must belong to `ask-hr`, not the old
   `ask-hr-activity` agent. Azure CLI must select the same subscription and tenant
   as this azd environment before running the directory configuration commands.
2. Run `.\scripts\configure_activity_sso.ps1` to bind the bot and delegated
   Search OAuth connection to the `ask-hr` identity and regenerate the Teams
   package. This changes Azure/Entra configuration and requires separate approval.
3. Publish the generated package using `.\scripts\publish_activity_teams.ps1`.
   Open the returned `installLink` and complete **Add/Update** in Teams before
   testing. Publishing updates the Developer Portal package, not an existing
   Teams installation. Verify sign-in, token exchange, an authorized HR answer,
   and follow-up history.

These commands change Azure, Entra, or Teams state. Editing this repository
does not perform them.

### Migrating the Activity POC

This section applies only to installations with the former separate
`ask-hr-activity` agent and bot.

<details>
<summary>Upgrade an existing separate Activity deployment</summary>

Deploy the unified `ask-hr` agent, follow [Teams setup](#teams-setup), and verify
both the existing Responses clients and the Teams flow. Only after both
protocols work in Azure, separately retire the old `ask-hr-activity` deployment
and its bot and the unused GPT-6.1 Sol deployment.

The existing Activity app ID, token-store federation settings, and package name
are retained for migration; they do not represent a second Foundry agent.
Reusing that app registration rebinds its SSO resource to the unified identity,
so coordinate Teams package publication with the SSO change rather than expecting
the old and new Activity bots to keep working concurrently. The separate web/M365
Responses adapter remains unchanged.

Bicep and `azure.yaml` no longer declare the separate Activity model or
`AZURE_AI_ACTIVITY_MODEL_DEPLOYMENT_NAME`. Removing those declarations does not
itself retire existing Azure resources or clear old local azd values.

</details>

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
GPT-5.4 is the default judge, separate from the shared generation model;
GPT-5.5 is a comparison judge. The rubric uses the question's language and accepts
honest acknowledgment of details absent from the policy. Its shared
assistant-authored answer key requires HR review before acceptance use.
See [evaluation limitations](../SHARING.md#evaluation-limitations) for scope
and known failures.

Review the saved report; add `--require-behavior-pass` when failed demo checks
must produce a nonzero exit code.

For a live Activity check, install the custom
`src\ask-hr\activityAppPackage\ask-hr-activity-poc.zip` package in Teams, sign in
with an authorized account in the project tenant, and verify an HR answer and a
follow-up. Do not use the generic azd-generated `appPackage.zip` for this SSO flow.
Anonymous Direct Line or emulator requests cannot substitute for this check:
Foundry can reject them before they reach the hosted handler because the Activity
caller has no valid Entra tenant. Keep `BotServiceRbac` enabled while testing.
