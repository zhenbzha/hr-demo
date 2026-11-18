"""Exercise unified-agent setup with all Azure commands intercepted."""

import json
import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ID = (
    "/subscriptions/test-subscription/resourceGroups/test-group/providers/"
    "Microsoft.CognitiveServices/accounts/test-account/projects/test-project"
)
ACTIVITY_ENDPOINT = (
    "https://example.services.ai.azure.com/api/projects/test-project/"
    "agents/ask-hr/endpoint/protocols/activityProtocol?api-version=test"
)
HARNESS = r"""
$ErrorActionPreference = 'Stop'
$values = $env:ASK_HR_TEST_VALUES | ConvertFrom-Json -AsHashtable
$calls = [System.Collections.Generic.List[object]]::new()
$observed = @{ bot = $null }

function azd {
    $global:LASTEXITCODE = 0
    $calls.Add(@{ command = 'azd'; arguments = @($args) })
    switch -Regex ($args -join ' ') {
        '^env get-value ' {
            if (-not $values.ContainsKey($args[2])) {
                $global:LASTEXITCODE = 1
                return
            }
            return $values[$args[2]]
        }
        '^env get-values ' { return ($values | ConvertTo-Json -Compress) }
        '^env set ' { $values[$args[2]] = $args[3]; return }
        '^ai agent show ask-hr --output json$' {
            return '{"instance_identity":{"client_id":"shared-client","principal_id":"shared-principal"}}'
        }
        default { throw "Unexpected azd command: $args" }
    }
}

function az {
    $global:LASTEXITCODE = 0
    $calls.Add(@{ command = 'az'; arguments = @($args) })
    switch -Regex ($args -join ' ') {
        '^bot show ' { return $values.TEST_BOT_ENDPOINT }
        '^account show ' {
            return (@{
                subscription = $values.AZURE_SUBSCRIPTION_ID
                tenant = $values.TEST_ACCOUNT_TENANT
            } | ConvertTo-Json -Compress)
        }
        '^account get-access-token ' { return 'synthetic-test-token' }
        '^ad app show .*--query appId' { return 'oauth-app' }
        '^ad app show .*--query id' { return 'oauth-object' }
        '^ad app show .*--query api\.' { return }
        '^ad sp list ' { return 'oauth-principal' }
        '^ad app federated-credential list ' { return '[]' }
        '^ad app federated-credential create ' { return }
        '^ad app permission admin-consent ' { return }
        '^role assignment list ' { return 'existing-assignment' }
        default { throw "Unexpected az command: $args" }
    }
}

function Invoke-WebRequest {
    param($Method, $Uri, $Headers, $ContentType, $Body)
    if ($Uri.StartsWith('https://graph.microsoft.com/')) { return }
    if ($Uri -like 'https://management.azure.com/*/botServices/*') {
        $observed.bot = @{ uri = $Uri; body = ($Body | ConvertFrom-Json) }
        throw 'Stopped before OAuth replacement and Teams package creation.'
    }
    throw "Unexpected HTTP request: $Method $Uri"
}

$failure = $null
try { & $env:ASK_HR_TEST_SCRIPT 6>$null }
catch { $failure = $_.Exception.Message }
@{
    calls = @($calls.ToArray())
    bot = $observed.bot
    error = $failure
} | ConvertTo-Json -Depth 20 -Compress
"""


class ActivityDeploymentTests(unittest.TestCase):
    def run_script(self, name, *, endpoint=ACTIVITY_ENDPOINT, account_tenant=None):
        values = {
            "AZURE_SUBSCRIPTION_ID": "test-subscription",
            "AZURE_TENANT_ID": "00000000-0000-0000-0000-000000000001",
            "AZURE_RESOURCE_GROUP": "test-group",
            "AZURE_AI_PROJECT_ID": PROJECT_ID,
            "AZURE_SEARCH_SERVICE_NAME": "test-search",
            "AGENT_ASK_HR_BOT_NAME": "shared-bot",
            "AGENT_ASK_HR_ACTIVITY_ENDPOINT": endpoint,
            "ASK_HR_ACTIVITY_BOT_APP_ID": "oauth-app",
            "ASK_HR_ACTIVITY_BOT_OAUTH_FEDERATED_ID": "existing-federation",
            "TEST_BOT_ENDPOINT": ACTIVITY_ENDPOINT,
            "TEST_ACCOUNT_TENANT": (
                account_tenant or "00000000-0000-0000-0000-000000000001"
            ),
        }
        result = subprocess.run(
            ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", HARNESS],
            cwd=ROOT,
            env={
                **os.environ,
                "ASK_HR_TEST_VALUES": json.dumps(values),
                "ASK_HR_TEST_SCRIPT": str(ROOT / "ask-hr" / "scripts" / name),
            },
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=True,
        )
        return json.loads(result.stdout)

    def test_sso_uses_shared_identity_scope_and_published_endpoint(self):
        result = self.run_script("configure_activity_sso.ps1")
        self.assertEqual(
            result["error"],
            "Stopped before OAuth replacement and Teams package creation.",
        )
        self.assertIn("/botServices/shared-bot?", result["bot"]["uri"])
        properties = result["bot"]["body"]["properties"]
        self.assertEqual(properties["endpoint"], ACTIVITY_ENDPOINT)
        self.assertEqual(properties["msaAppId"], "shared-client")
        roles = [
            call["arguments"]
            for call in result["calls"]
            if call["command"] == "az"
            and call["arguments"][:3] == ["role", "assignment", "list"]
        ]
        self.assertEqual(len(roles), 2)
        for arguments in roles:
            self.assertEqual(
                arguments[arguments.index("--scope") + 1],
                PROJECT_ID + "/agents/ask-hr",
            )

    def test_stale_separate_agent_endpoint_stops_before_any_azure_mutation(self):
        result = self.run_script(
            "configure_activity_sso.ps1",
            endpoint=ACTIVITY_ENDPOINT.replace("/ask-hr/", "/ask-hr-activity/"),
        )
        self.assertIn("must belong to the unified ask-hr agent", result["error"])
        self.assertTrue(all(call["command"] == "azd" for call in result["calls"]))
        self.assertIsNone(result["bot"])

    def test_reads_registered_bot_endpoint_when_azd_omits_activity_output(self):
        result = self.run_script("configure_activity_sso.ps1", endpoint=None)
        self.assertEqual(
            result["error"],
            "Stopped before OAuth replacement and Teams package creation.",
        )
        self.assertEqual(
            result["bot"]["body"]["properties"]["endpoint"], ACTIVITY_ENDPOINT
        )
        lookup = next(
            call["arguments"] for call in result["calls"]
            if call["command"] == "az" and call["arguments"][:2] == ["bot", "show"]
        )
        self.assertIn("test-subscription", lookup)
        self.assertIn("shared-bot", lookup)

    def test_wrong_cli_tenant_stops_before_directory_changes(self):
        result = self.run_script(
            "configure_activity_sso.ps1", account_tenant="another-tenant"
        )
        self.assertIn("Select the azd environment subscription", result["error"])
        self.assertFalse(any(
            call["command"] == "az" and call["arguments"][0] == "ad"
            for call in result["calls"]
        ))
        self.assertIsNone(result["bot"])

    def test_search_role_hook_targets_only_shared_identity(self):
        result = self.run_script("grant_agent_search_role.ps1")
        self.assertIsNone(result["error"])
        shows = [
            call["arguments"]
            for call in result["calls"]
            if call["command"] == "azd" and call["arguments"][:3] == ["ai", "agent", "show"]
        ]
        self.assertEqual(shows, [["ai", "agent", "show", "ask-hr", "--output", "json"]])
        roles = [call["arguments"] for call in result["calls"] if call["command"] == "az"]
        self.assertEqual(len(roles), 1)
        self.assertIn("shared-principal", roles[0])
        self.assertIn("test-subscription", roles[0])


if __name__ == "__main__":
    unittest.main()
