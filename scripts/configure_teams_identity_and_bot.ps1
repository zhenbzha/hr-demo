[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

function Invoke-AzdValue {
    param([Parameter(Mandatory)][string] $Name)

    $value = azd env get-value $Name 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($value)) {
        throw "The azd environment value '$Name' is required."
    }
    return $value.Trim()
}

function Invoke-Checked {
    param(
        [scriptblock] $Script,
        [string] $Description
    )

    & $Script
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

function Invoke-AzRestJson {
    param(
        [string] $Method,
        [string] $Url,
        [string] $Body,
        [string] $Description
    )

    $bodyFile = Join-Path ([System.IO.Path]::GetTempPath()) "$([guid]::NewGuid()).json"
    try {
        Set-Content `
            -Path $bodyFile `
            -Value $Body `
            -Encoding utf8NoBOM `
            -NoNewline
        Invoke-Checked {
            az rest `
                --method $Method `
                --url $Url `
                --headers 'Content-Type=application/json' `
                --body "@$bodyFile" `
                --output none
        } $Description
    }
    finally {
        if (Test-Path $bodyFile) {
            Remove-Item $bodyFile -Force
        }
    }
}

function Ensure-FederatedCredential {
    param(
        [string] $AppId,
        [string] $Name,
        [string] $Description,
        [string] $Issuer,
        [string] $Subject
    )

    $existing = az ad app federated-credential list `
        --id $AppId `
        --output json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to list federated credentials for $AppId."
    }
    $matchingCredential = @($existing) |
        Where-Object { $_.name -eq $Name } |
        Select-Object -First 1
    $expectedAudience = 'api://AzureADTokenExchange'
    if (
        $matchingCredential -and
        $matchingCredential.issuer -eq $Issuer -and
        $matchingCredential.subject -eq $Subject -and
        @($matchingCredential.audiences).Count -eq 1 -and
        $matchingCredential.audiences[0] -eq $expectedAudience
    ) {
        return
    }
    if ($matchingCredential) {
        Invoke-Checked {
            az ad app federated-credential delete `
                --id $AppId `
                --federated-credential-id $matchingCredential.id
        } "Delete stale federated credential $Name"
    }

    $parameters = @{
        name        = $Name
        description = $Description
        issuer      = $Issuer
        subject     = $Subject
        audiences   = @($expectedAudience)
    } | ConvertTo-Json -Compress
    $parametersFile = Join-Path ([System.IO.Path]::GetTempPath()) "$([guid]::NewGuid()).json"
    try {
        Set-Content `
            -Path $parametersFile `
            -Value $parameters `
            -Encoding utf8NoBOM `
            -NoNewline
        Invoke-Checked {
            az ad app federated-credential create `
                --id $AppId `
                --parameters "@$parametersFile" `
                --output none
        } "Create federated credential $Name"
    }
    finally {
        if (Test-Path $parametersFile) {
            Remove-Item $parametersFile -Force
        }
    }
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

$subscriptionId = Invoke-AzdValue 'AZURE_SUBSCRIPTION_ID'
$resourceGroup = Invoke-AzdValue 'AZURE_RESOURCE_GROUP'
$tenantId = Invoke-AzdValue 'AZURE_TENANT_ID'
$containerAppName = Invoke-AzdValue 'SERVICE_M365_ADAPTER_NAME'
$containerAppFqdn = Invoke-AzdValue 'M365_ADAPTER_FQDN'
$managedIdentityClientId = Invoke-AzdValue 'M365_ADAPTER_CLIENT_ID'
$managedIdentityPrincipalId = Invoke-AzdValue 'M365_ADAPTER_PRINCIPAL_ID'
$foundryEndpoint = Invoke-AzdValue 'FOUNDRY_AGENT_RESPONSES_ENDPOINT'

$appDisplayName = 'Ask HR M365 Adapter'
$botName = "ask-hr-sso-$($subscriptionId.Substring(0, 8))"
$oauthConnectionName = 'ask-hr-search-sso'
$searchResourceAppId = '880da380-985e-4198-81b9-e05b1cc53158'
$searchDelegatedPermissionId = 'a4165a31-5d9e-4120-bd1e-9d88c66fd3b8'
$issuer = "https://login.microsoftonline.com/$tenantId/v2.0"
$botEndpoint = "https://$containerAppFqdn/api/messages"
$stateFile = Join-Path $PSScriptRoot '..\.azure\teams-deployment-state.json'

$state = $null
if (Test-Path $stateFile) {
    $state = Get-Content $stateFile -Raw | ConvertFrom-Json
}

$appId = $state.botAppId
if ($appId) {
    $existingStateApp = az ad app show --id $appId --query appId -o tsv 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $existingStateApp) {
        $appId = $null
    }
}
if (-not $appId) {
    $escapedDisplayName = $appDisplayName.Replace("'", "''")
    $appId = az ad app list `
        --filter "displayName eq '$escapedDisplayName'" `
        --query '[0].appId' `
        -o tsv
    if ($LASTEXITCODE -ne 0) {
        throw 'Unable to search for the Ask HR adapter Entra application.'
    }
}
if (-not $appId) {
    $appId = az ad app create `
        --display-name $appDisplayName `
        --sign-in-audience AzureADMyOrg `
        --query appId `
        -o tsv
    if ($LASTEXITCODE -ne 0 -or -not $appId) {
        throw 'Unable to create the Ask HR adapter Entra application.'
    }
}

$servicePrincipalId = az ad sp list `
    --filter "appId eq '$appId'" `
    --query '[0].id' `
    -o tsv
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to query the adapter service principal.'
}
if (-not $servicePrincipalId) {
    Invoke-Checked {
        az ad sp create --id $appId --output none
    } 'Create adapter service principal'
}

$appObjectId = az ad app show --id $appId --query id -o tsv
if ($LASTEXITCODE -ne 0 -or -not $appObjectId) {
    throw 'Unable to resolve the adapter application object ID.'
}

$scopeId = az ad app show `
    --id $appId `
    --query "api.oauth2PermissionScopes[?value=='defaultScopes'].id | [0]" `
    -o tsv
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to query the adapter OAuth scope.'
}
if (-not $scopeId) {
    $scopeId = [guid]::NewGuid().ToString()
}

$preAuthorizedClientIds = @(
    '1fec8e78-bce4-4aaf-ab1b-5451cc387264',
    '5e3ce6c0-2b1f-4285-8d4b-75ee78787346',
    '4765445b-32c6-49b0-83e6-1d93765276ca',
    '0ec893e0-5785-4de6-99da-4ed124e5296c',
    'd3590ed6-52b3-4102-aeff-aad2292ab01c',
    'bc59ab01-8403-45c6-8796-ac3ef710b3e3',
    '27922004-5251-4030-b22d-91ecd9a37ea4'
)

$apiConfiguration = @{
    requestedAccessTokenVersion = 2
    oauth2PermissionScopes = @(
        @{
            id                          = $scopeId
            adminConsentDescription     = 'Allow Microsoft 365 to access Ask HR as the signed-in user.'
            adminConsentDisplayName     = 'Access Ask HR as a user'
            isEnabled                   = $true
            type                        = 'User'
            userConsentDescription      = 'Allow Microsoft 365 to access Ask HR on your behalf.'
            userConsentDisplayName      = 'Access Ask HR as a user'
            value                       = 'defaultScopes'
        }
    )
}

$graphPatch = @{
    identifierUris = @("api://botid-$appId")
    api = $apiConfiguration
    web = @{
        redirectUris = @(
            'https://token.botframework.com/.auth/web/redirect'
        )
    }
} | ConvertTo-Json -Depth 10 -Compress

Invoke-AzRestJson `
    -Method PATCH `
    -Url "https://graph.microsoft.com/v1.0/applications/$appObjectId" `
    -Body $graphPatch `
    -Description 'Configure Ask HR Teams SSO application'

$preAuthorizationPatch = @{
    api = @{
        requestedAccessTokenVersion = 2
        oauth2PermissionScopes = $apiConfiguration.oauth2PermissionScopes
        preAuthorizedApplications = @(
            $preAuthorizedClientIds | ForEach-Object {
                @{
                    appId = $_
                    delegatedPermissionIds = @($scopeId)
                }
            }
        )
    }
} | ConvertTo-Json -Depth 10 -Compress

Invoke-AzRestJson `
    -Method PATCH `
    -Url "https://graph.microsoft.com/v1.0/applications/$appObjectId" `
    -Body $preAuthorizationPatch `
    -Description 'Preauthorize Microsoft 365 SSO clients'

$searchPermissionPatch = @{
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
} | ConvertTo-Json -Depth 8 -Compress

Invoke-AzRestJson `
    -Method PATCH `
    -Url "https://graph.microsoft.com/v1.0/applications/$appObjectId" `
    -Body $searchPermissionPatch `
    -Description 'Configure Azure AI Search delegated permission'

Invoke-Checked {
    az ad app permission admin-consent --id $appId --output none
} 'Grant tenant admin consent'

$oboSecretName = 'ask-hr-obo-client-secret'
$existingOboSecret = az containerapp secret list `
    --resource-group $resourceGroup `
    --name $containerAppName `
    --query "[?name=='$oboSecretName'].name | [0]" `
    -o tsv
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to query adapter Container App secrets.'
}
if ($existingOboSecret -and $state.botAppId -ne $appId) {
    Invoke-Checked {
        az containerapp secret remove `
            --resource-group $resourceGroup `
            --name $containerAppName `
            --secret-names $oboSecretName `
            --output none
    } 'Remove stale OBO credential'
    $existingOboSecret = $null
}
$oboCredentials = @(
    az ad app credential list --id $appId --output json |
        ConvertFrom-Json |
        Where-Object { $_.displayName -eq 'ask-hr-obo' }
)
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to query Ask HR OBO credentials.'
}
$deployedOboCredentialKeyId = az containerapp show `
    --resource-group $resourceGroup `
    --name $containerAppName `
    --query "properties.template.containers[0].env[?name=='ASK_HR_OBO_CREDENTIAL_KEY_ID'].value | [0]" `
    --output tsv
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to query the deployed Ask HR OBO credential ID.'
}
$recordedOboCredentialKeyId = if ($deployedOboCredentialKeyId) {
    $deployedOboCredentialKeyId.Trim()
}
else {
    $state.oboCredentialKeyId
}
$rotationThreshold = [DateTimeOffset]::UtcNow.AddDays(30)
$activeOboCredential = @($oboCredentials) |
    Where-Object {
        $_.keyId -eq $recordedOboCredentialKeyId -and
        [DateTimeOffset]$_.endDateTime -gt $rotationThreshold
    } |
    Select-Object -First 1
$rotateOboCredential = -not $existingOboSecret -or -not $activeOboCredential
if ($rotateOboCredential) {
    $credential = az ad app credential reset `
        --id $appId `
        --append `
        --display-name 'ask-hr-obo' `
        --years 1 `
        -o json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or -not $credential.password) {
        throw 'Unable to create the Ask HR OBO credential.'
    }
    try {
        Invoke-Checked {
            az containerapp secret set `
                --resource-group $resourceGroup `
                --name $containerAppName `
                --secrets "$oboSecretName=$($credential.password)" `
                --output none
        } 'Store OBO credential in Container Apps'
        $activeOboCredential = @(
            az ad app credential list --id $appId --output json |
                ConvertFrom-Json |
                Where-Object { $_.displayName -eq 'ask-hr-obo' } |
                Sort-Object startDateTime -Descending
        ) | Select-Object -First 1
        if (-not $activeOboCredential) {
            throw 'Unable to resolve the newly created Ask HR OBO credential.'
        }
    }
    finally {
        $credential.password = $null
    }
}

Ensure-FederatedCredential `
    -AppId $appId `
    -Name 'adapter-runtime' `
    -Description 'Container Apps managed identity authenticates the Ask HR adapter application.' `
    -Issuer $issuer `
    -Subject $managedIdentityPrincipalId

$oauthFederatedId = $null
if ($state) {
    $oauthFederatedId = $state.botOAuthFederatedId
}
if (-not $oauthFederatedId) {
    $oauthFederatedId = [guid]::NewGuid().ToString()
}

$tenantBytes = ([guid]$tenantId).ToByteArray()
$tenantEncoded = [Convert]::ToBase64String($tenantBytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
$botTokenStoreApp = '9ExAW52n_ky4ZiS_jhpJIQ'
$botTokenStoreSubject = "/eid1/c/pub/t/$tenantEncoded/a/$botTokenStoreApp/$oauthFederatedId"

Ensure-FederatedCredential `
    -AppId $appId `
    -Name 'bot-token-store' `
    -Description 'Azure Bot token store obtains exchangeable user tokens.' `
    -Issuer $issuer `
    -Subject $botTokenStoreSubject

$botBody = @{
    location = 'global'
    kind = 'azurebot'
    sku = @{ name = 'F0' }
    properties = @{
        displayName = 'Ask HR'
        endpoint = $botEndpoint
        msaAppId = $appId
        msaAppTenantId = $tenantId
        msaAppType = 'SingleTenant'
        publicNetworkAccess = 'Enabled'
    }
} | ConvertTo-Json -Depth 8 -Compress

Invoke-AzRestJson `
    -Method PUT `
    -Url "https://management.azure.com/subscriptions/$subscriptionId/resourceGroups/$resourceGroup/providers/Microsoft.BotService/botServices/$botName`?api-version=2022-09-15" `
    -Body $botBody `
    -Description 'Create Ask HR Azure Bot'

$channelBody = @{
    location = 'global'
    properties = @{
        channelName = 'MsTeamsChannel'
    }
} | ConvertTo-Json -Depth 5 -Compress

Invoke-AzRestJson `
    -Method PUT `
    -Url "https://management.azure.com/subscriptions/$subscriptionId/resourceGroups/$resourceGroup/providers/Microsoft.BotService/botServices/$botName/channels/MsTeamsChannel?api-version=2022-09-15" `
    -Body $channelBody `
    -Description 'Create Microsoft Teams channel'

$existingConnection = az bot authsetting list `
    --resource-group $resourceGroup `
    --name $botName `
    --query "[?name=='$oauthConnectionName'].name | [0]" `
    -o tsv
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to query Azure Bot OAuth settings.'
}
if ($existingConnection) {
    Invoke-Checked {
        az bot authsetting delete `
            --resource-group $resourceGroup `
            --name $botName `
            --setting-name $oauthConnectionName `
            --output none
    } 'Replace Azure Bot OAuth setting'
}

Invoke-Checked {
    az bot authsetting create `
        --resource-group $resourceGroup `
        --name $botName `
        --setting-name $oauthConnectionName `
        --client-id $appId `
        --client-secret '""' `
        --provider-scope-string "api://botid-$appId/defaultScopes" `
        --service Aadv2WithFic `
        --parameters `
            tenantId=$tenantId `
            tokenExchangeUrl="api://botid-$appId" `
            uniqueIdentifier=$oauthFederatedId `
        --output none
} 'Create Azure Bot OAuth setting'

$environmentVariables = @(
    "connections__serviceConnection__settings__clientId=$appId",
    'connections__serviceConnection__settings__authType=FederatedCredentials',
    "connections__serviceConnection__settings__federatedClientId=$managedIdentityClientId",
    "connections__serviceConnection__settings__tenantId=$tenantId",
    "connections__serviceConnection__settings__authorityEndpoint=https://login.microsoftonline.com/$tenantId",
    'connections__serviceConnection__settings__scopes=https://api.botframework.com/.default',
    'connectionsMap__0__connection=serviceConnection',
    'connectionsMap__0__serviceUrl=*',
    "connections__oboConnection__settings__clientId=$appId",
    'connections__oboConnection__settings__authType=ClientSecret',
    "connections__oboConnection__settings__clientSecret=secretref:$oboSecretName",
    "connections__oboConnection__settings__tenantId=$tenantId",
    "connections__oboConnection__settings__authorityEndpoint=https://login.microsoftonline.com/$tenantId",
    'connectionsMap__1__connection=oboConnection',
    'connectionsMap__1__serviceUrl=obo',
    'AgentApplication__UserAuthorization__Handlers__search__Settings__title=HR policy access',
    'AgentApplication__UserAuthorization__Handlers__search__Settings__text=Continue with your Microsoft 365 account',
    "AgentApplication__UserAuthorization__Handlers__search__Settings__azureBotOAuthConnectionName=$oauthConnectionName",
    'AgentApplication__UserAuthorization__Handlers__search__Settings__oboConnectionName=oboConnection',
    'AgentApplication__UserAuthorization__Handlers__search__Settings__oboScopes=https://search.azure.com/.default',
    'AgentApplication__UserAuthorization__Handlers__search__Settings__enableSso=true',
    'DEBUG=agents:*:error,agents:*:warn',
    "AZURE_CLIENT_ID=$managedIdentityClientId",
    "BOT_APP_ID=$appId",
    "ASK_HR_OBO_CREDENTIAL_KEY_ID=$($activeOboCredential.keyId)",
    "FOUNDRY_AGENT_RESPONSES_ENDPOINT=$foundryEndpoint"
)

Invoke-Checked {
    az containerapp update `
        --resource-group $resourceGroup `
        --name $containerAppName `
        --set-env-vars $environmentVariables `
        --output none
} 'Configure Ask HR adapter environment'

Wait-ContainerAppReady `
    -ResourceGroup $resourceGroup `
    -Name $containerAppName

foreach ($obsoleteCredential in @($oboCredentials)) {
    if ($obsoleteCredential.keyId -eq $activeOboCredential.keyId) {
        continue
    }
    Invoke-Checked {
        az ad app credential delete `
            --id $appId `
            --key-id $obsoleteCredential.keyId
    } "Delete obsolete Ask HR OBO credential $($obsoleteCredential.keyId)"
}

$stateDirectory = Split-Path -Parent $stateFile
New-Item -ItemType Directory -Path $stateDirectory -Force | Out-Null
@{
    botAppId = $appId
    botName = $botName
    botDomain = $containerAppFqdn
    oauthConnectionName = $oauthConnectionName
    botOAuthFederatedId = $oauthFederatedId
    oboCredentialKeyId = $activeOboCredential.keyId
} | ConvertTo-Json | Set-Content -Path $stateFile -Encoding utf8NoBOM

Invoke-Checked {
    azd env set ASK_HR_TEAMS_BOT_APP_ID $appId
} 'Persist Ask HR Teams Bot app ID'
Invoke-Checked {
    azd env set ASK_HR_TEAMS_BOT_NAME $botName
} 'Persist Ask HR Teams Bot name'
Invoke-Checked {
    azd env set ASK_HR_TEAMS_OAUTH_CONNECTION $oauthConnectionName
} 'Persist Ask HR Teams OAuth connection'

& (Join-Path $PSScriptRoot '..\apps\ask-hr-m365-adapter\scripts\package-manifest.ps1') `
    -BotAppId $appId `
    -BotDomain $containerAppFqdn

Write-Host "Configured Ask HR Teams SSO for Bot app $appId."
