[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

function Invoke-AzdValue {
    param([Parameter(Mandatory)][string]$Name)

    $value = azd env get-value $Name 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($value)) {
        throw "The azd environment value '$Name' is required."
    }
    return $value.Trim()
}

function Wait-ContainerAppReady {
    param(
        [string] $ResourceGroup,
        [string] $Name,
        [timespan] $Timeout = [timespan]::FromMinutes(5)
    )

    $deadline = [DateTimeOffset]::UtcNow.Add($Timeout)
    do {
        $status = az containerapp show `
            --resource-group $ResourceGroup `
            --name $Name `
            --query '{latest:properties.latestRevisionName,ready:properties.latestReadyRevisionName,state:properties.provisioningState}' `
            --output json | ConvertFrom-Json
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to query Container App '$Name'."
        }
        if (
            $status.state -eq 'Succeeded' -and
            $status.latest -and
            $status.latest -eq $status.ready
        ) {
            return
        }
        Start-Sleep -Seconds 5
    } while ([DateTimeOffset]::UtcNow -lt $deadline)

    throw "Container App '$Name' did not become ready before the timeout."
}

$keyVaultName = Invoke-AzdValue 'AZURE_KEY_VAULT_NAME'
$resourceGroup = Invoke-AzdValue 'AZURE_RESOURCE_GROUP'
$tenantId = Invoke-AzdValue 'AZURE_TENANT_ID'
$containerAppName = Invoke-AzdValue 'SERVICE_WEB_NAME'
$foundryAgentEndpoint = Invoke-AzdValue 'FOUNDRY_AGENT_ENDPOINT'
$agentModelDeployments = [ordered]@{
    AZURE_AI_MODEL_DEPLOYMENT_NAME = Invoke-AzdValue 'AZURE_AI_MODEL_DEPLOYMENT_NAME'
    ASK_HR_EVAL_MODEL_DEPLOYMENT_NAME = Invoke-AzdValue 'ASK_HR_EVAL_MODEL_DEPLOYMENT_NAME'
    AZURE_AI_CLASSIFIER_MODEL_DEPLOYMENT_NAME = Invoke-AzdValue 'AZURE_AI_CLASSIFIER_MODEL_DEPLOYMENT_NAME'
}
$agentEnvironmentName = azd env get-value ASK_HR_AGENT_ENV_NAME 2>$null
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($agentEnvironmentName)) {
    $agentEnvironmentName = 'ask-hr-dev'
    azd env set ASK_HR_AGENT_ENV_NAME $agentEnvironmentName
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to persist the hosted-agent azd environment name.'
    }
}
$agentEnvironmentName = $agentEnvironmentName.Trim()
$webUri = Invoke-AzdValue 'SERVICE_WEB_URI'
$redirectUris = @(
    "$webUri/api/auth/callback"
    'http://localhost:3000/api/auth/callback'
)

$clientId = azd env get-value ENTRA_CLIENT_ID 2>$null
if ($LASTEXITCODE -ne 0) {
    $clientId = ''
}
$clientId = $clientId.Trim()

if ($clientId) {
    $app = az ad app show --id $clientId -o json | ConvertFrom-Json
    if (-not $app) {
        throw "ENTRA_CLIENT_ID '$clientId' does not resolve to an app registration."
    }
    az ad app update `
        --id $clientId `
        --display-name 'Ask HR Web' `
        --sign-in-audience AzureADMultipleOrgs `
        --web-redirect-uris $redirectUris
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to update the Ask HR Entra application.'
    }
}
else {
    $app = az ad app create `
        --display-name 'Ask HR Web' `
        --sign-in-audience AzureADMultipleOrgs `
        --web-redirect-uris $redirectUris `
        -o json | ConvertFrom-Json
    if (-not $app.appId) {
        throw 'Failed to create the Ask HR Entra application.'
    }
    $clientId = $app.appId
    azd env set ENTRA_CLIENT_ID $clientId
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to persist ENTRA_CLIENT_ID in the azd environment.'
    }
}

$servicePrincipal = az ad sp show --id $clientId -o json 2>$null | ConvertFrom-Json
if (-not $servicePrincipal) {
    az ad sp create --id $clientId 1>$null
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to create the Ask HR service principal.'
    }
}

$searchResourceAppId = '880da380-985e-4198-81b9-e05b1cc53158'
$searchDelegatedPermissionId = 'a4165a31-5d9e-4120-bd1e-9d88c66fd3b8'
$configuredPermissions = az ad app permission list `
    --id $clientId `
    --output json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to query the Ask HR Search delegated permission.'
}
$searchPermissionCount = @(
    $configuredPermissions |
        Where-Object { $_.resourceAppId -eq $searchResourceAppId } |
        ForEach-Object { $_.resourceAccess } |
        Where-Object {
            $_.id -eq $searchDelegatedPermissionId -and
            $_.type -eq 'Scope'
        }
).Count
if ($searchPermissionCount -ne 1) {
    if ($searchPermissionCount -gt 0) {
        az ad app permission delete `
            --id $clientId `
            --api $searchResourceAppId
        if ($LASTEXITCODE -ne 0) {
            throw 'Failed to normalize the Azure AI Search delegated permission.'
        }
    }
    az ad app permission add `
        --id $clientId `
        --api $searchResourceAppId `
        --api-permissions "$searchDelegatedPermissionId=Scope" `
        --output none
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to add the Azure AI Search delegated permission.'
    }
}

az ad app permission admin-consent --id $clientId --output none
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to grant tenant consent for Ask HR delegated Search access.'
}

$clientServicePrincipalId = az ad sp show --id $clientId --query id --output tsv
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($clientServicePrincipalId)) {
    throw 'Failed to resolve the Ask HR service principal.'
}
$searchServicePrincipalId = az ad sp show --id $searchResourceAppId --query id --output tsv
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($searchServicePrincipalId)) {
    throw 'Failed to resolve the Azure AI Search service principal.'
}

function Test-SearchDelegatedConsent {
    $grants = az rest `
        --method get `
        --url "https://graph.microsoft.com/v1.0/servicePrincipals/$($clientServicePrincipalId.Trim())/oauth2PermissionGrants" `
        --output json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to verify delegated Search consent.'
    }

    return @($grants.value) | Where-Object {
        $_.resourceId -eq $searchServicePrincipalId.Trim() -and
        $_.consentType -eq 'AllPrincipals' -and
        @($_.scope -split ' ') -contains 'user_impersonation'
    }
}

if (-not (Test-SearchDelegatedConsent)) {
    az ad app permission grant `
        --id $clientId `
        --api $searchResourceAppId `
        --scope 'user_impersonation' `
        --output none
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to create the tenant-wide delegated Search permission grant.'
    }
}

if (-not (Test-SearchDelegatedConsent)) {
    throw 'Azure AI Search delegated permission is declared but tenant consent is not effective.'
}

$existingCredentials = az ad app credential list --id $clientId -o json | ConvertFrom-Json
$credential = az ad app credential reset `
    --id $clientId `
    --append `
    --display-name 'ask-hr-azd' `
    --years 1 `
    -o json | ConvertFrom-Json
if (-not $credential.password) {
    throw 'Failed to create the Ask HR application credential.'
}
$activeCredential = @(
    az ad app credential list --id $clientId --output json |
        ConvertFrom-Json |
        Where-Object { $_.displayName -eq 'ask-hr-azd' } |
        Sort-Object startDateTime -Descending
) | Select-Object -First 1
if ($LASTEXITCODE -ne 0 -or -not $activeCredential) {
    throw 'Failed to resolve the newly created Ask HR application credential.'
}

try {
    az deployment group create `
        --name 'ask-hr-entra-secret' `
        --resource-group $resourceGroup `
        --template-file 'infra/modules/key-vault-secret.bicep' `
        --parameters `
            vaultName=$keyVaultName `
            secretName='entra-client-secret' `
            secretValue=$credential.password `
        --only-show-errors `
        --output none
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to deploy the Entra credential through Azure Resource Manager.'
    }

    az containerapp secret set `
        --name $containerAppName `
        --resource-group $resourceGroup `
        --secrets "entra-client-secret=$($credential.password)" `
        --only-show-errors `
        --output none
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to configure the Container App Entra secret.'
    }

    az containerapp update `
        --name $containerAppName `
        --resource-group $resourceGroup `
        --set-env-vars `
            "ENTRA_TENANT_ID=$tenantId" `
            "ENTRA_CLIENT_ID=$clientId" `
            'ENTRA_CLIENT_SECRET=secretref:entra-client-secret' `
            "ENTRA_CREDENTIAL_KEY_ID=$($activeCredential.keyId)" `
            "ENTRA_REDIRECT_URI=$webUri/api/auth/callback" `
            "APP_BASE_URL=$webUri" `
            'SESSION_COOKIE_NAME=ask_hr_session' `
            "FOUNDRY_AGENT_ENDPOINT=$foundryAgentEndpoint" `
            'FOUNDRY_AGENT_NAME=ask-hr' `
        --only-show-errors `
        --output none
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to configure the Container App application settings.'
    }

    Wait-ContainerAppReady `
        -ResourceGroup $resourceGroup `
        -Name $containerAppName

    foreach ($existingCredential in @($existingCredentials)) {
        if (
            $existingCredential.displayName -ne 'ask-hr-azd' -or
            $existingCredential.keyId -eq $activeCredential.keyId
        ) {
            continue
        }
        az ad app credential delete `
            --id $clientId `
            --key-id $existingCredential.keyId
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to remove stale Ask HR credential $($existingCredential.keyId)."
        }
    }
}
finally {
    $credential.password = $null
}

azd env set ENTRA_TENANT_ID $tenantId
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to persist the Entra tenant ID.'
}
azd env set ENTRA_CLIENT_SECRET_NAME 'entra-client-secret'
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to persist the Entra client secret name.'
}

azd env set -C ask-hr -e $agentEnvironmentName ASK_HR_WEB_BASE_URL $webUri
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to synchronize the Ask HR web URL to the agent environment.'
}

foreach ($deployment in $agentModelDeployments.GetEnumerator()) {
    azd env set -C ask-hr -e $agentEnvironmentName $deployment.Key $deployment.Value
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to synchronize '$($deployment.Key)' to the agent environment."
    }
}

Write-Host "Configured Ask HR Entra application $clientId."
