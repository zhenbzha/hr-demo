targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('Azure Developer CLI environment name.')
param environmentName string

@description('Primary Azure region for every Ask HR resource.')
param location string = 'francecentral'

@description('Object ID of the user or service principal running azd.')
param principalId string

@description('Object ID of the user or service principal running document ingestion.')
param ingestionPrincipalId string

@description('Grant the deployment principal permission to send represented-user identities for local diagnostics.')
param assignDeployerImpersonationRole bool = false

@description('Container image deployed to the Ask HR web Container App.')
param webImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@description('Container image deployed to the Ask HR Microsoft 365 adapter.')
param m365AdapterImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('Resource group for the isolated Ask HR demo.')
param resourceGroupName string = 'rg-ask-hr-demo'

var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))
var tags = {
  'azd-env-name': environmentName
  application: 'ask-hr'
  workload: 'hr-demo'
}

resource resourceGroup 'Microsoft.Resources/resourceGroups@2023-07-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module monitoring './modules/monitoring.bicep' = {
  name: 'monitoring'
  scope: resourceGroup
  params: {
    location: location
    tags: tags
  }
}

module storage './modules/storage.bicep' = {
  name: 'storage'
  scope: resourceGroup
  params: {
    location: location
    name: 'dlsaskhr${resourceToken}'
    tags: tags
  }
}

module keyVault './modules/key-vault.bicep' = {
  name: 'key-vault'
  scope: resourceGroup
  params: {
    location: location
    name: 'kv-askhr-${resourceToken}'
    tags: tags
  }
}

module foundry './modules/foundry.bicep' = {
  name: 'foundry'
  scope: resourceGroup
  params: {
    accountName: 'aif-askhr-${resourceToken}'
    activityDeploymentName: 'ask-hr-gpt-6.1-sol'
    classifierDeploymentName: 'ask-hr-gpt-4.1-mini'
    evaluationDeploymentName: 'ask-hr-gpt-5.4'
    generationDeploymentName: 'ask-hr-gpt-6-astra'
    generationModelCapacity: 100
    location: location
    projectName: 'ask-hr-project'
    tags: tags
  }
}

module search './modules/search.bicep' = {
  name: 'search'
  scope: resourceGroup
  params: {
    location: location
    name: 'srch-askhr-${resourceToken}'
    tags: tags
  }
}

module documentIntelligence './modules/document-intelligence.bicep' = {
  name: 'document-intelligence'
  scope: resourceGroup
  params: {
    location: location
    name: 'di-askhr-${resourceToken}'
    tags: tags
  }
}

module containerApps './modules/container-apps.bicep' = {
  name: 'container-apps'
  scope: resourceGroup
  params: {
    applicationInsightsConnectionString: monitoring.outputs.applicationInsightsConnectionString
    containerAppName: 'ca-ask-hr-web'
    containerAppsEnvironmentName: 'cae-ask-hr-demo'
    containerRegistryName: 'craskhr${resourceToken}'
    foundryProjectEndpoint: foundry.outputs.projectEndpoint
    generationDeploymentName: foundry.outputs.generationDeploymentName
    keyVaultUri: keyVault.outputs.uri
    location: location
    logAnalyticsWorkspaceResourceId: monitoring.outputs.logAnalyticsResourceId
    searchEndpoint: search.outputs.endpoint
    storageBlobEndpoint: storage.outputs.blobEndpoint
    storageDfsEndpoint: storage.outputs.dfsEndpoint
    storageFileSystemName: storage.outputs.fileSystemName
    tags: tags
    webImage: webImage
  }
}

module userImpersonationRole './modules/user-impersonation-role.bicep' = {
  name: 'ask-hr-user-impersonation-role'
  params: {
    assignableScopeId: resourceGroup.id
  }
}

module m365Adapter './modules/m365-adapter.bicep' = {
  name: 'm365-adapter'
  scope: resourceGroup
  params: {
    applicationInsightsConnectionString: monitoring.outputs.applicationInsightsConnectionString
    containerAppsEnvironmentName: containerApps.outputs.containerAppsEnvironmentName
    containerRegistryName: containerApps.outputs.containerRegistryName
    foundryAccountName: foundry.outputs.accountName
    foundryProjectName: foundry.outputs.projectName
    impersonationRoleDefinitionId: userImpersonationRole.outputs.roleDefinitionId
    location: location
    serviceImageName: m365AdapterImage
    tags: tags
  }
}

module roleAssignments './modules/role-assignments.bicep' = {
  name: 'role-assignments'
  scope: resourceGroup
  params: {
    containerRegistryName: containerApps.outputs.containerRegistryName
    deployerPrincipalId: principalId
    documentIntelligenceName: documentIntelligence.outputs.name
    foundryAccountName: foundry.outputs.accountName
    foundryProjectName: foundry.outputs.projectName
    foundryProjectPrincipalId: foundry.outputs.projectPrincipalId
    impersonationRoleDefinitionId: userImpersonationRole.outputs.roleDefinitionId
    assignDeployerImpersonationRole: assignDeployerImpersonationRole
    keyVaultName: keyVault.outputs.name
    searchPrincipalId: search.outputs.principalId
    searchServiceName: search.outputs.name
    storageAccountName: storage.outputs.name
    ingestionPrincipalId: ingestionPrincipalId
    webPrincipalId: containerApps.outputs.webPrincipalId
  }
}

module storageLock './modules/storage-lock.bicep' = {
  name: 'storage-lock'
  scope: resourceGroup
  params: {
    storageAccountName: storage.outputs.name
  }
  // ReadOnly would block account-scoped role assignments if applied earlier.
  dependsOn: [
    roleAssignments
  ]
}

output AZURE_LOCATION string = location
output AZURE_RESOURCE_GROUP string = resourceGroup.name
output AZURE_SUBSCRIPTION_ID string = subscription().subscriptionId
output AZURE_TENANT_ID string = subscription().tenantId

output AZURE_AI_FOUNDRY_ACCOUNT_NAME string = foundry.outputs.accountName
output AZURE_AI_CLASSIFIER_MODEL_DEPLOYMENT_NAME string = foundry.outputs.classifierDeploymentName
output AZURE_AI_PROJECT_NAME string = foundry.outputs.projectName
output AZURE_AI_PROJECT_ID string = foundry.outputs.projectId
output AZURE_AI_PROJECT_ENDPOINT string = foundry.outputs.projectEndpoint
output AZURE_AI_MODEL_DEPLOYMENT_NAME string = foundry.outputs.generationDeploymentName
output AZURE_AI_ACTIVITY_MODEL_DEPLOYMENT_NAME string = foundry.outputs.activityDeploymentName
output ASK_HR_EVAL_MODEL_DEPLOYMENT_NAME string = foundry.outputs.evaluationDeploymentName
output AZURE_OPENAI_ENDPOINT string = foundry.outputs.openAiEndpoint
output AZURE_OPENAI_EMBEDDING_DEPLOYMENT string = foundry.outputs.embeddingDeploymentName
output AZURE_OPENAI_EMBEDDING_DIMENSIONS int = 1024

output AZURE_SEARCH_SERVICE_NAME string = search.outputs.name
output AZURE_SEARCH_ENDPOINT string = search.outputs.endpoint
output AZURE_SEARCH_INDEX_NAME string = 'ask-hr-docs'
output AZURE_SEARCH_KNOWLEDGE_SOURCE string = 'ask-hr-ks'
output AZURE_SEARCH_KNOWLEDGE_BASE string = 'ask-hr-kb'

output AZURE_DOCUMENT_INTELLIGENCE_NAME string = documentIntelligence.outputs.name
output AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT string = documentIntelligence.outputs.endpoint

output AZURE_STORAGE_ACCOUNT_NAME string = storage.outputs.name
output AZURE_STORAGE_BLOB_ENDPOINT string = storage.outputs.blobEndpoint
output AZURE_STORAGE_DFS_ENDPOINT string = storage.outputs.dfsEndpoint
output AZURE_STORAGE_FILE_SYSTEM string = storage.outputs.fileSystemName

output AZURE_KEY_VAULT_NAME string = keyVault.outputs.name
output AZURE_KEY_VAULT_ENDPOINT string = keyVault.outputs.uri

output AZURE_CONTAINER_REGISTRY_NAME string = containerApps.outputs.containerRegistryName
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = containerApps.outputs.containerRegistryLoginServer
output AZURE_CONTAINER_APPS_ENVIRONMENT_NAME string = containerApps.outputs.containerAppsEnvironmentName
output SERVICE_WEB_NAME string = containerApps.outputs.containerAppName
output SERVICE_WEB_URI string = containerApps.outputs.containerAppUri
output AZURE_CLIENT_ID string = containerApps.outputs.webClientId

output SERVICE_M365_ADAPTER_NAME string = m365Adapter.outputs.containerAppName
output SERVICE_M365_ADAPTER_URI string = m365Adapter.outputs.containerAppUri
output M365_ADAPTER_FQDN string = m365Adapter.outputs.containerAppFqdn
output M365_ADAPTER_CLIENT_ID string = m365Adapter.outputs.managedIdentityClientId
output M365_ADAPTER_PRINCIPAL_ID string = m365Adapter.outputs.managedIdentityPrincipalId
output M365_ADAPTER_RESOURCE_ID string = m365Adapter.outputs.managedIdentityResourceId
output FOUNDRY_AGENT_RESPONSES_ENDPOINT string = m365Adapter.outputs.foundryAgentResponsesEndpoint

output APPLICATIONINSIGHTS_CONNECTION_STRING string = monitoring.outputs.applicationInsightsConnectionString
output APPLICATIONINSIGHTS_NAME string = monitoring.outputs.applicationInsightsName
output AZURE_LOG_ANALYTICS_WORKSPACE_NAME string = monitoring.outputs.logAnalyticsName
