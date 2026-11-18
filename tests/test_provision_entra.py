"""Offline post-provisioning tests with every az and azd command intercepted."""

import json
import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL_BINDINGS = {
    "AZURE_AI_MODEL_DEPLOYMENT_NAME": "ask-hr-gpt-6-astra",
    "ASK_HR_EVAL_MODEL_DEPLOYMENT_NAME": "ask-hr-gpt-5.4",
    "AZURE_AI_CLASSIFIER_MODEL_DEPLOYMENT_NAME": "configured-classifier",
}
HOOK_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$state = $env:ASK_HR_TEST_STATE | ConvertFrom-Json -AsHashtable
$writes = [System.Collections.Generic.List[object]]::new()
$azureCalls = 0

function azd {
    $global:LASTEXITCODE = 0
    if ($args[0] -eq 'env' -and $args[1] -eq 'get-value') {
        if (-not $state.root.ContainsKey($args[2])) {
            $global:LASTEXITCODE = 1
            return
        }
        return $state.root[$args[2]]
    }
    if ($args[0] -eq 'env' -and $args[1] -eq 'set') {
        if ($args[2] -eq '-C') {
            if ($args.Count -ne 8 -or $args[4] -ne '-e') {
                throw "Unexpected nested azd arguments: $args"
            }
            $writes.Add(@{
                project = $args[3]
                environment = $args[5]
                name = $args[6]
                value = $args[7]
            })
            if ($args[6] -eq $state.failWrite) {
                $global:LASTEXITCODE = 1
                return
            }
            $state.nested[$args[6]] = $args[7]
            return
        }
        $state.root[$args[2]] = $args[3]
        return
    }
    throw "Unexpected azd command: $args"
}

function az {
    $global:LASTEXITCODE = 0
    $script:azureCalls++
    switch -Regex ($args -join ' ') {
        '^ad app show ' { return '{"appId":"test-client"}' }
        '^ad sp show .*--query id' {
            if ($args -contains '880da380-985e-4198-81b9-e05b1cc53158') {
                return 'test-search-principal'
            }
            return 'test-client-principal'
        }
        '^ad sp show ' { return '{"id":"test-client-principal"}' }
        '^ad app permission list ' {
            return '[{"resourceAppId":"880da380-985e-4198-81b9-e05b1cc53158","resourceAccess":[{"id":"a4165a31-5d9e-4120-bd1e-9d88c66fd3b8","type":"Scope"}]}]'
        }
        '^rest ' {
            return '{"value":[{"resourceId":"test-search-principal","consentType":"AllPrincipals","scope":"user_impersonation"}]}'
        }
        '^ad app credential list ' {
            return '[{"keyId":"test-key","displayName":"ask-hr-azd","startDateTime":"2026-01-01T00:00:00Z"}]'
        }
        '^ad app credential reset ' { return '{"password":"synthetic-test-value"}' }
        '^containerapp show ' {
            return '{"latest":"test-revision","ready":"test-revision","state":"Succeeded"}'
        }
        '^(ad app update |ad app permission admin-consent |deployment group create |containerapp secret set |containerapp update )' {
            return
        }
        default { throw "Unexpected az command: $args" }
    }
}

$failure = $null
try {
    & $env:ASK_HR_TEST_HOOK 6>$null
}
catch {
    $failure = $_.Exception.Message
}
@{
    nested = $state.nested
    writes = @($writes.ToArray())
    azureCalls = $azureCalls
    error = $failure
} | ConvertTo-Json -Depth 5 -Compress
"""


class ProvisionEntraTests(unittest.TestCase):
    def run_hook(self, *, nested=None, missing=None, fail_write=None, agent_env=None):
        root_values = {
            "AZURE_KEY_VAULT_NAME": "test-vault",
            "AZURE_RESOURCE_GROUP": "test-resource-group",
            "AZURE_TENANT_ID": "test-tenant",
            "SERVICE_WEB_NAME": "test-web",
            "SERVICE_WEB_URI": "https://web.example.test",
            "FOUNDRY_AGENT_ENDPOINT": "https://agent.example.test/responses",
            "ENTRA_CLIENT_ID": "test-client",
            **MODEL_BINDINGS,
        }
        if agent_env is not None:
            root_values["ASK_HR_AGENT_ENV_NAME"] = agent_env
        if missing is not None:
            del root_values[missing]
        result = subprocess.run(
            [
                "pwsh",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                HOOK_HARNESS,
            ],
            cwd=ROOT,
            env={
                **os.environ,
                "ASK_HR_TEST_HOOK": str(ROOT / "scripts" / "provision_entra.ps1"),
                "ASK_HR_TEST_STATE": json.dumps(
                    {
                        "root": root_values,
                        "nested": {} if nested is None else nested,
                        "failWrite": fail_write,
                    }
                ),
            },
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=True,
        )
        return json.loads(result.stdout)

    def assert_synchronized(self, result, environment):
        self.assertIsNone(result["error"])
        expected = {
            **MODEL_BINDINGS,
            "ASK_HR_WEB_BASE_URL": "https://web.example.test",
        }
        self.assertEqual(
            {name: result["nested"].get(name) for name in expected}, expected
        )
        self.assertCountEqual([write["name"] for write in result["writes"]], expected)
        for write in result["writes"]:
            self.assertEqual(write["project"], "ask-hr")
            self.assertEqual(write["environment"], environment)

    def test_updates_the_selected_existing_sol_environment(self):
        result = self.run_hook(
            nested={
                "AZURE_AI_MODEL_DEPLOYMENT_NAME": "ask-hr-gpt-6.1-sol",
                "UNRELATED_SETTING": "preserved",
            },
            agent_env="selected-agent-environment",
        )
        self.assert_synchronized(result, "selected-agent-environment")
        self.assertEqual(result["nested"]["UNRELATED_SETTING"], "preserved")

    def test_initializes_bindings_in_the_default_agent_environment(self):
        self.assert_synchronized(self.run_hook(), "ask-hr-dev")

    def test_requires_model_outputs_before_any_azure_changes(self):
        for name in MODEL_BINDINGS:
            with self.subTest(name=name):
                result = self.run_hook(missing=name)
                self.assertEqual(
                    result["error"], f"The azd environment value '{name}' is required."
                )
                self.assertEqual(result["azureCalls"], 0)
                self.assertEqual(result["writes"], [])

    def test_failed_model_synchronization_stops_the_hook(self):
        for name in MODEL_BINDINGS:
            with self.subTest(name=name):
                result = self.run_hook(fail_write=name)
                self.assertIsNotNone(result["error"])
                self.assertIn(name, result["error"])
                self.assertEqual(result["writes"][-1]["name"], name)
                self.assertNotIn(name, result["nested"])


if __name__ == "__main__":
    unittest.main()
