# Ask HR hosted agent

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

- `AZURE_AI_MODEL_DEPLOYMENT_NAME`
- `AZURE_AI_PROJECT_ENDPOINT`
- `AZURE_SEARCH_ENDPOINT`
- `AZURE_SEARCH_KNOWLEDGE_BASE`
- `AZURE_SEARCH_KNOWLEDGE_SOURCE`

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

The repository-level quality runner exercises all hard requirements against the
deployed Responses endpoint:

```powershell
python ..\scripts\validate_hosted_agent.py
```

The suite covers region clarification, multilingual questions, local-over-
Global precedence, unsupported regions, policy gaps, and source citations.
