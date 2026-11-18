$ErrorActionPreference = 'Stop'
$env:AZURE_DEV_USER_AGENT = 'microsoft_foundry_skill'

$values = azd env get-values --output json | ConvertFrom-Json

$scope = @(
    '/subscriptions/', $values.AZURE_SUBSCRIPTION_ID,
    '/resourceGroups/', $values.AZURE_RESOURCE_GROUP,
    '/providers/Microsoft.Search/searchServices/',
    $values.AZURE_SEARCH_SERVICE_NAME
) -join ''

$agentName = 'ask-hr'
$agentJson = azd ai agent show $agentName --output json 2>$null
if ($LASTEXITCODE -ne 0 -or -not $agentJson) {
    throw "Unable to inspect the deployed $agentName agent."
}
$agent = $agentJson | ConvertFrom-Json
$principalId = $agent.instance_identity.principal_id
if (-not $principalId) {
    throw "The deployed $agentName agent has no instance identity principal ID."
}

$existing = az role assignment list `
    --subscription $values.AZURE_SUBSCRIPTION_ID `
    --assignee-object-id $principalId `
    --scope $scope `
    --query "[?roleDefinitionName=='Search Index Data Reader'].id | [0]" `
    --output tsv
if ($LASTEXITCODE -ne 0) {
    throw "Unable to inspect Search roles for $agentName."
}

if (-not $existing) {
    az role assignment create `
        --subscription $values.AZURE_SUBSCRIPTION_ID `
        --assignee-object-id $principalId `
        --scope $scope `
        --assignee-principal-type ServicePrincipal `
        --role 'Search Index Data Reader' `
        --output none
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to grant Search Index Data Reader to $agentName."
    }
}

Write-Host "$agentName identity has Search Index Data Reader on $scope"
