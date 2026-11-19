# Coding Agent Instructions

This is the `ask-hr` Microsoft Foundry hosted-agent project. One Python process
serves both Responses and Activity; the adapters share the HR pipeline.
Follow the repository-root `AGENTS.md` and read the Microsoft Foundry skill
before working on the agent.

## Key files

- `azure.yaml`: agent service, protocols, model bindings, and code deployment.
- `src\ask-hr\main.py`: combined SDK host and Responses handler.
- `src\ask-hr\activity_app.py`: Teams Activity handler.
- `src\ask-hr\ask_hr_pipeline.py`: shared classification, retrieval, and generation.
- `src\ask-hr\permission_contract.py`: trusted identity and Search authorization.
- Agent dependencies: `pyproject.toml`, `uv.lock`, and `requirements.txt`, all
  under `src\ask-hr`.

The configured deployment uses `codeConfiguration` with Python 3.13 and bundled
dependencies. The retained `src\ask-hr\Dockerfile` is not the active deployment
path. The agent's `.agentignore` controls code packaging.

## Development workflow

Use the agent's own Python environment, separate from the root
ingestion/evaluation environment. Run project-scoped azd commands from this
`ask-hr` directory, with the configured environment and service values.

```powershell
$env:AZURE_DEV_USER_AGENT = "microsoft_foundry_skill"
azd ai agent run --no-client
```

Set the user-agent value in the shell only; do not persist it in project
configuration. Deploy only when requested:

```powershell
$env:AZURE_DEV_USER_AGENT = "microsoft_foundry_skill"
azd deploy ask-hr --no-prompt
```

For CLI text invocations of this multi-protocol service, select Responses
explicitly. Bare unauthenticated requests are not valid smoke tests: follow the
[authentication contract](README.md#authentication-and-retrieval). Use the
documented Teams sign-in flow for Activity, not an anonymous emulator substitute.

See [README.md](README.md) for setup, deployment, and evaluation entry points.
Do not change the approved architecture or deployment order without explicit
user approval.

## References

- [Hosted agents overview](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents)
- [Microsoft Foundry skill source](https://github.com/microsoft/azure-skills/tree/main/.github/plugins/azure-skills/skills/microsoft-foundry)
- [Microsoft Foundry Skill](https://learn.microsoft.com/en-us/azure/foundry/how-to/develop/use-microsoft-foundry-skill)