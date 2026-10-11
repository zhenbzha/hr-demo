[CmdletBinding()]
param(
    [switch] $PublishTeamsApp
)

$ErrorActionPreference = 'Stop'
$env:AZURE_DEV_USER_AGENT = 'microsoft_foundry_skill'
. (Join-Path $PSScriptRoot 'script_helpers.ps1')

function Invoke-AzdValue {
    param([Parameter(Mandatory)][string] $Name)

    $value = azd env get-value $Name 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($value)) {
        throw "The azd environment value '$Name' is required."
    }
    return $value.Trim()
}

function Invoke-OptionalAzdValue {
    param([Parameter(Mandatory)][string] $Name)

    $value = azd env get-value $Name 2>$null
    if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($value)) {
        return $value.Trim()
    }
    return $null
}

function Invoke-AzRestJson {
    param(
        [Parameter(Mandatory)][string] $Method,
        [Parameter(Mandatory)][string] $Url,
        [Parameter(Mandatory)][object] $Body,
        [Parameter(Mandatory)][string] $Description
    )

    $resource = if ($Url.StartsWith('https://graph.microsoft.com/')) {
        'https://graph.microsoft.com/'
    }
    else {
        'https://management.azure.com/'
    }
    $token = az account get-access-token `
        --resource $resource `
        --query accessToken `
        --output tsv
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($token)) {
        throw "Unable to acquire an access token for $Description."
    }
    Invoke-WebRequest `
        -Method $Method `
        -Uri $Url `
        -Headers @{ Authorization = "Bearer $token" } `
        -ContentType 'application/json' `
        -Body ($Body | ConvertTo-Json -Depth 30 -Compress) |
        Out-Null
}

function Ensure-FederatedCredential {
    param(
        [Parameter(Mandatory)][string] $AppId,
        [Parameter(Mandatory)][string] $Name,
        [Parameter(Mandatory)][string] $Issuer,
        [Parameter(Mandatory)][string] $Subject,
        [Parameter(Mandatory)][string] $Description
    )

    $existing = az ad app federated-credential list `
        --id $AppId `
        --output json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to list federated credentials for $AppId."
    }
    $credential = @($existing) |
        Where-Object { $_.name -eq $Name } |
        Select-Object -First 1
    $audience = 'api://AzureADTokenExchange'
    if (
        $credential -and
        $credential.issuer -eq $Issuer -and
        $credential.subject -eq $Subject -and
        @($credential.audiences).Count -eq 1 -and
        $credential.audiences[0] -eq $audience
    ) {
        return
    }
    if ($credential) {
        Invoke-Checked {
            az ad app federated-credential delete `
                --id $AppId `
                --federated-credential-id $credential.id
        } "Delete stale federated credential $Name"
    }

    $parameters = @{
        name = $Name
        description = $Description
        issuer = $Issuer
        subject = $Subject
        audiences = @($audience)
    } | ConvertTo-Json -Compress
    $file = Join-Path ([IO.Path]::GetTempPath()) "$([guid]::NewGuid()).json"
    try {
        Set-Content -LiteralPath $file -Value $parameters -Encoding utf8NoBOM
        Invoke-Checked {
            az ad app federated-credential create `
                --id $AppId `
                --parameters "@$file" `
                --output none
        } "Create federated credential $Name"
    }
    finally {
        Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
    }
}

$subscriptionId = Invoke-AzdValue 'AZURE_SUBSCRIPTION_ID'
$tenantId = Invoke-AzdValue 'AZURE_TENANT_ID'
$resourceGroup = Invoke-AzdValue 'AZURE_RESOURCE_GROUP'
$botName = Invoke-AzdValue 'AGENT_ASK_HR_ACTIVITY_BOT_NAME'
$searchResourceAppId = '880da380-985e-4198-81b9-e05b1cc53158'
$searchDelegatedPermissionId = 'a4165a31-5d9e-4120-bd1e-9d88c66fd3b8'
$oauthConnectionName = 'ask-hr-search-sso'
$displayName = 'Ask HR Activity POC'
$issuer = "https://login.microsoftonline.com/$tenantId/v2.0"
$agent = azd ai agent show ask-hr-activity --output json | ConvertFrom-Json
if (
    -not $agent.instance_identity.client_id -or
    -not $agent.instance_identity.principal_id
) {
    throw 'The Activity hosted agent instance identity is unavailable.'
}
$agentClientId = $agent.instance_identity.client_id
$agentPrincipalId = $agent.instance_identity.principal_id
$ssoResource = "api://botid-$agentClientId"

$appId = Invoke-OptionalAzdValue 'ASK_HR_ACTIVITY_BOT_APP_ID'
if ($appId) {
    $existingApp = az ad app show --id $appId --query appId -o tsv 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $existingApp) {
        $appId = $null
    }
}
if (-not $appId) {
    $escapedName = $displayName.Replace("'", "''")
    $appId = az ad app list `
        --filter "displayName eq '$escapedName'" `
        --query '[0].appId' `
        --output tsv
}
if (-not $appId) {
    $appId = az ad app create `
        --display-name $displayName `
        --sign-in-audience AzureADMyOrg `
        --query appId `
        --output tsv
}
if ($LASTEXITCODE -ne 0 -or -not $appId) {
    throw 'Unable to create or resolve the Activity POC application.'
}

$servicePrincipal = az ad sp list `
    --filter "appId eq '$appId'" `
    --query '[0].id' `
    --output tsv
if (-not $servicePrincipal) {
    Invoke-Checked {
        az ad sp create --id $appId --output none
    } 'Create Activity POC service principal'
    $servicePrincipal = az ad sp list `
        --filter "appId eq '$appId'" `
        --query '[0].id' `
        --output tsv
}
$appObjectId = az ad app show --id $appId --query id -o tsv
if ($LASTEXITCODE -ne 0 -or -not $appObjectId) {
    throw 'Unable to resolve the Activity POC application object ID.'
}
$scopeId = az ad app show `
    --id $appId `
    --query "api.oauth2PermissionScopes[?value=='defaultScopes'].id | [0]" `
    --output tsv
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to inspect the Activity POC delegated scope.'
}
$scopeExists = -not [string]::IsNullOrWhiteSpace($scopeId)
if (-not $scopeId) {
    $scopeId = [guid]::NewGuid().ToString()
}

$scope = @{
    id = $scopeId
    adminConsentDescription = 'Allow Microsoft 365 to access Ask HR Activity as the signed-in user.'
    adminConsentDisplayName = 'Access Ask HR Activity as a user'
    isEnabled = $true
    type = 'User'
    userConsentDescription = 'Allow Microsoft 365 to access Ask HR Activity on your behalf.'
    userConsentDisplayName = 'Access Ask HR Activity as a user'
    value = 'defaultScopes'
}
if ($scopeExists) {
    $disabledScope = $scope.Clone()
    $disabledScope.isEnabled = $false
    Invoke-AzRestJson `
        -Method PATCH `
        -Url "https://graph.microsoft.com/v1.0/applications/$appObjectId" `
        -Body @{
            api = @{
                oauth2PermissionScopes = @($disabledScope)
            }
        } `
        -Description 'Temporarily disable Activity POC delegated scope'
}
Invoke-AzRestJson `
    -Method PATCH `
    -Url "https://graph.microsoft.com/v1.0/applications/$appObjectId" `
    -Body @{
        identifierUris = @($ssoResource)
        api = @{
            requestedAccessTokenVersion = 2
            oauth2PermissionScopes = @($scope)
            preAuthorizedApplications = @()
        }
        web = @{
            redirectUris = @('https://token.botframework.com/.auth/web/redirect')
        }
        requiredResourceAccess = @(
            @{
                resourceAppId = $searchResourceAppId
                resourceAccess = @(
                    @{
                        id = $searchDelegatedPermissionId
                        type = 'Scope'
                    }
                )
            }
        )
    } `
    -Description 'Configure Activity POC application'

$preAuthorizedClientIds = @(
    '1fec8e78-bce4-4aaf-ab1b-5451cc387264',
    '5e3ce6c0-2b1f-4285-8d4b-75ee78787346',
    '4765445b-32c6-49b0-83e6-1d93765276ca',
    '0ec893e0-5785-4de6-99da-4ed124e5296c',
    'd3590ed6-52b3-4102-aeff-aad2292ab01c',
    'bc59ab01-8403-45c6-8796-ac3ef710b3e3',
    '27922004-5251-4030-b22d-91ecd9a37ea4'
)
Invoke-AzRestJson `
    -Method PATCH `
    -Url "https://graph.microsoft.com/v1.0/applications/$appObjectId" `
    -Body @{
        api = @{
            requestedAccessTokenVersion = 2
            oauth2PermissionScopes = @($scope)
            preAuthorizedApplications = @(
                $preAuthorizedClientIds | ForEach-Object {
                    @{
                        appId = $_
                        delegatedPermissionIds = @($scopeId)
                    }
                }
            )
        }
    } `
    -Description 'Preauthorize Microsoft 365 clients'

Invoke-Checked {
    az ad app permission admin-consent --id $appId --output none
} 'Grant Activity POC admin consent'

Invoke-Checked {
    azd env set ASK_HR_ACTIVITY_BOT_APP_ID $appId --no-prompt
} 'Persist Activity POC app ID'

$federatedId = Invoke-OptionalAzdValue `
    'ASK_HR_ACTIVITY_BOT_OAUTH_FEDERATED_ID'
if (-not $federatedId) {
    $federatedId = [guid]::NewGuid().ToString()
    Invoke-Checked {
        azd env set `
            ASK_HR_ACTIVITY_BOT_OAUTH_FEDERATED_ID `
            $federatedId `
            --no-prompt
    } 'Persist token-store federated identifier'
}
$tenantEncoded = [Convert]::ToBase64String(
    ([guid]$tenantId).ToByteArray()
).TrimEnd('=').Replace('+', '-').Replace('/', '_')
$subject = (
    "/eid1/c/pub/t/$tenantEncoded/a/9ExAW52n_ky4ZiS_jhpJIQ/$federatedId"
)
Ensure-FederatedCredential `
    -AppId $appId `
    -Name 'ask-hr-activity-bot-token-store' `
    -Issuer $issuer `
    -Subject $subject `
    -Description 'Azure Bot token store obtains exchangeable user tokens.'

$agentEndpoint = $agent.agent_endpoints.Agent
$agentScope = (
    "/subscriptions/$subscriptionId/resourceGroups/$resourceGroup/" +
    "providers/Microsoft.CognitiveServices/accounts/" +
    "aif-askhr-3yw342ef3vpq2/projects/ask-hr-project/" +
    "agents/ask-hr-activity"
)
foreach ($consumerPrincipal in @($servicePrincipal, $agentPrincipalId)) {
    $consumerRole = az role assignment list `
        --assignee-object-id $consumerPrincipal `
        --scope $agentScope `
        --role 'Foundry Agent Consumer' `
        --query '[0].id' `
        --output tsv
    if (-not $consumerRole) {
        Invoke-Checked {
            az role assignment create `
                --assignee-object-id $consumerPrincipal `
                --assignee-principal-type ServicePrincipal `
                --role 'Foundry Agent Consumer' `
                --scope $agentScope `
                --output none
        } 'Grant Activity POC endpoint access'
    }
}

$botBody = @{
    location = 'global'
    kind = 'azurebot'
    sku = @{ name = 'F0' }
    properties = @{
        displayName = 'Ask HR Activity POC'
        endpoint = (
            "https://aif-askhr-3yw342ef3vpq2.services.ai.azure.com/" +
            "api/projects/ask-hr-project/agents/ask-hr-activity/" +
            "endpoint/protocols/activityProtocol?api-version=2025-05-15-preview"
        )
        msaAppId = $agentClientId
        msaAppTenantId = $tenantId
        msaAppType = 'SingleTenant'
        publicNetworkAccess = 'Enabled'
    }
}
Invoke-AzRestJson `
    -Method PUT `
    -Url (
        "https://management.azure.com/subscriptions/$subscriptionId/" +
        "resourceGroups/$resourceGroup/providers/Microsoft.BotService/" +
        "botServices/$botName`?api-version=2022-09-15"
    ) `
    -Body $botBody `
    -Description 'Bind Activity Bot to its Entra application'

$existingConnection = az bot authsetting list `
    --resource-group $resourceGroup `
    --name $botName `
    --query "[?name=='$oauthConnectionName'].name | [0]" `
    --output tsv
if ($existingConnection) {
    Invoke-Checked {
        az bot authsetting delete `
            --resource-group $resourceGroup `
            --name $botName `
            --setting-name $oauthConnectionName `
            --output none
    } 'Replace Activity Bot OAuth setting'
}
Invoke-Checked {
    az bot authsetting create `
        --resource-group $resourceGroup `
        --name $botName `
        --setting-name $oauthConnectionName `
        --client-id $appId `
        --client-secret '""' `
        --provider-scope-string 'https://search.azure.com/.default' `
        --service Aadv2WithFic `
        --parameters `
            tenantId=$tenantId `
            tokenExchangeUrl=$ssoResource `
            uniqueIdentifier=$federatedId `
        --output none
} 'Create Activity Bot OAuth setting'

$packageDirectory = Join-Path `
    $PSScriptRoot `
    '..\src\ask-hr\activityAppPackage'
$zipPath = Join-Path $packageDirectory 'ask-hr-activity-poc.zip'
if (Test-Path $packageDirectory) {
    Remove-Item -LiteralPath $packageDirectory -Recurse -Force
}
New-Item -ItemType Directory -Path $packageDirectory | Out-Null
$manifest = @{
    '$schema' = 'https://developer.microsoft.com/json-schemas/teams/v1.22/MicrosoftTeams.schema.json'
    manifestVersion = '1.22'
    version = '5.0.1'
    id = $appId
    developer = @{
        name = 'Microsoft'
        websiteUrl = 'https://www.microsoft.com'
        privacyUrl = 'https://privacy.microsoft.com'
        termsOfUseUrl = 'https://www.microsoft.com/legal/terms-of-use'
    }
    icons = @{ color = 'color.png'; outline = 'outline.png' }
    name = @{ short = 'Ask HR Activity'; full = 'Ask HR Activity POC' }
    description = @{
        short = 'Tests Ask HR as a Foundry Activity agent.'
        full = 'Tests Teams SSO, Search OBO, and Ask HR answers directly in a Foundry Activity agent.'
    }
    accentColor = '#047857'
    copilotAgents = @{
        customEngineAgents = @(@{ id = $agentClientId; type = 'bot' })
    }
    bots = @(
        @{
            botId = $agentClientId
            scopes = @('personal')
            supportsFiles = $false
            isNotificationOnly = $false
        }
    )
    permissions = @('identity', 'messageTeamMembers')
    validDomains = @(
        'token.botframework.com',
        ([Uri]$agentEndpoint).Host
    )
    webApplicationInfo = @{
        id = $appId
        resource = $ssoResource
    }
} | ConvertTo-Json -Depth 20
Set-Content `
    -LiteralPath (Join-Path $packageDirectory 'manifest.json') `
    -Value $manifest `
    -Encoding utf8NoBOM `
    -NoNewline
$icons = Join-Path `
    $PSScriptRoot `
    '..\..\apps\ask-hr-m365-adapter\appManifest'
Copy-Item (Join-Path $icons 'color.png') $packageDirectory
Copy-Item (Join-Path $icons 'outline.png') $packageDirectory
Compress-Archive `
    -Path (Join-Path $packageDirectory 'manifest.json'),
          (Join-Path $packageDirectory 'color.png'),
          (Join-Path $packageDirectory 'outline.png') `
    -DestinationPath $zipPath `
    -Force

Write-Host "Activity POC app ID: $appId"
Write-Host "Activity Bot: $botName"
Write-Host "Teams package: $zipPath"

if ($PublishTeamsApp) {
    & (Join-Path $PSScriptRoot 'publish_activity_teams.ps1') `
        -PackagePath $zipPath
}
