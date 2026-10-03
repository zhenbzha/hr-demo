$ErrorActionPreference = 'Stop'
$env:AZURE_DEV_USER_AGENT = 'microsoft_foundry_skill'

$values = azd env get-values --output json | ConvertFrom-Json
$agent = azd ai agent show ask-hr --output json | ConvertFrom-Json

$principalId = $agent.instance_identity.principal_id
if (-not $principalId) {
    throw 'The deployed Ask HR agent has no instance identity principal ID.'
}

$scope = @(
    '/subscriptions/', $values.AZURE_SUBSCRIPTION_ID,
    '/resourceGroups/', $values.AZURE_RESOURCE_GROUP,
    '/providers/Microsoft.Search/searchServices/',
    $values.AZURE_SEARCH_SERVICE_NAME
) -join ''

$existing = az role assignment list `
    --assignee-object-id $principalId `
    --scope $scope `
    --query "[?roleDefinitionName=='Search Index Data Reader'].id | [0]" `
    --output tsv

if (-not $existing) {
    az role assignment create `
        --assignee-object-id $principalId `
        --assignee-principal-type ServicePrincipal `
        --role 'Search Index Data Reader' `
        --scope $scope `
        --output none
}

Write-Host "Ask HR agent identity has Search Index Data Reader on $scope"
