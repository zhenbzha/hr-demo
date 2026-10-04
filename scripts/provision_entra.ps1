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

function New-RandomSecret {
    $bytes = [byte[]]::new(32)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [Convert]::ToBase64String($bytes)
}

$keyVaultName = Invoke-AzdValue 'AZURE_KEY_VAULT_NAME'
$resourceGroup = Invoke-AzdValue 'AZURE_RESOURCE_GROUP'
$tenantId = Invoke-AzdValue 'AZURE_TENANT_ID'
$containerAppName = Invoke-AzdValue 'SERVICE_WEB_NAME'
$foundryAgentEndpoint = Invoke-AzdValue 'FOUNDRY_AGENT_ENDPOINT'
$webUri = Invoke-AzdValue 'SERVICE_WEB_URI'
$redirectUris = @(
    "$webUri/api/auth/callback/azure-ad"
    'http://localhost:3000/api/auth/callback/azure-ad'
)

$nextAuthSecretName = azd env get-value NEXTAUTH_SECRET_NAME 2>$null
$createNextAuthSecret = $LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($nextAuthSecretName)
if ($createNextAuthSecret) {
    $nextAuthSecretName = 'nextauth-secret'
    $nextAuthSecret = New-RandomSecret
}
else {
    $nextAuthSecretName = $nextAuthSecretName.Trim()
    $nextAuthSecret = $null
}

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

$existingCredentials = az ad app credential list --id $clientId -o json | ConvertFrom-Json
foreach ($existingCredential in @($existingCredentials)) {
    if ($existingCredential.displayName -eq 'ask-hr-azd') {
        az ad app credential delete --id $clientId --key-id $existingCredential.keyId
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to remove stale Ask HR credential $($existingCredential.keyId)."
        }
    }
}

$credential = az ad app credential reset `
    --id $clientId `
    --append `
    --display-name 'ask-hr-azd' `
    --years 1 `
    -o json | ConvertFrom-Json
if (-not $credential.password) {
    throw 'Failed to create the Ask HR application credential.'
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

    if ($createNextAuthSecret) {
        az deployment group create `
            --name 'ask-hr-nextauth-secret' `
            --resource-group $resourceGroup `
            --template-file 'infra/modules/key-vault-secret.bicep' `
            --parameters `
                vaultName=$keyVaultName `
                secretName=$nextAuthSecretName `
                secretValue=$nextAuthSecret `
            --only-show-errors `
            --output none
        if ($LASTEXITCODE -ne 0) {
            throw 'Failed to deploy the NextAuth session secret through Azure Resource Manager.'
        }

        az containerapp secret set `
            --name $containerAppName `
            --resource-group $resourceGroup `
            --secrets "$nextAuthSecretName=$nextAuthSecret" `
            --only-show-errors `
            --output none
        if ($LASTEXITCODE -ne 0) {
            throw 'Failed to configure the Container App NextAuth session secret.'
        }
    }

    az containerapp update `
        --name $containerAppName `
        --resource-group $resourceGroup `
        --set-env-vars `
            "ENTRA_TENANT_ID=$tenantId" `
            "ENTRA_CLIENT_ID=$clientId" `
            'ENTRA_CLIENT_SECRET=secretref:entra-client-secret' `
            "NEXTAUTH_URL=$webUri" `
            "NEXTAUTH_SECRET=secretref:$nextAuthSecretName" `
            "FOUNDRY_AGENT_ENDPOINT=$foundryAgentEndpoint" `
            'FOUNDRY_AGENT_NAME=ask-hr' `
        --only-show-errors `
        --output none
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to configure the Container App application settings.'
    }
}
finally {
    $credential.password = $null
    $nextAuthSecret = $null
}

azd env set ENTRA_TENANT_ID $tenantId
azd env set ENTRA_CLIENT_SECRET_NAME 'entra-client-secret'
azd env set NEXTAUTH_SECRET_NAME $nextAuthSecretName
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to persist Entra configuration in the azd environment.'
}

Write-Host "Configured Ask HR Entra application $clientId."
