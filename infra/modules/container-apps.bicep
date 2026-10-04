targetScope = 'resourceGroup'

param applicationInsightsConnectionString string
param containerAppName string
param containerAppsEnvironmentName string
param containerRegistryName string
param foundryProjectEndpoint string
param generationDeploymentName string
param keyVaultUri string
param location string
param logAnalyticsWorkspaceResourceId string
param searchEndpoint string
param storageBlobEndpoint string
param storageDfsEndpoint string
param storageFileSystemName string
param tags object = {}
param webImage string

module containerRegistry 'br/public:avm/res/container-registry/registry:0.13.1' = {
  params: {
    acrAdminUserEnabled: false
    acrSku: 'Basic'
    enableTelemetry: false
    location: location
    name: containerRegistryName
    networkRuleSetDefaultAction: 'Allow'
    publicNetworkAccess: 'Enabled'
    tags: tags
  }
}

module containerAppsEnvironment 'br/public:avm/res/app/managed-environment:0.13.0' = {
  params: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsWorkspaceResourceId: logAnalyticsWorkspaceResourceId
    }
    enableTelemetry: false
    location: location
    name: containerAppsEnvironmentName
    publicNetworkAccess: 'Enabled'
    tags: tags
    zoneRedundant: false
  }
}

resource webIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-ask-hr-web'
  location: location
  tags: tags
}

module containerApp 'br/public:avm/res/app/container-app:0.23.0' = {
  params: {
    containers: [
      {
        env: [
          {
            name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
            value: applicationInsightsConnectionString
          }
          {
            name: 'AZURE_AI_MODEL_DEPLOYMENT_NAME'
            value: generationDeploymentName
          }
          {
            name: 'AZURE_AI_PROJECT_ENDPOINT'
            value: foundryProjectEndpoint
          }
          {
            name: 'AZURE_CLIENT_ID'
            value: webIdentity.properties.clientId
          }
          {
            name: 'AZURE_KEY_VAULT_ENDPOINT'
            value: keyVaultUri
          }
          {
            name: 'AZURE_SEARCH_ENDPOINT'
            value: searchEndpoint
          }
          {
            name: 'AZURE_STORAGE_BLOB_ENDPOINT'
            value: storageBlobEndpoint
          }
          {
            name: 'AZURE_STORAGE_DFS_ENDPOINT'
            value: storageDfsEndpoint
          }
          {
            name: 'AZURE_STORAGE_FILE_SYSTEM'
            value: storageFileSystemName
          }
        ]
        image: webImage
        name: 'web'
        resources: {
          cpu: json('0.5')
          memory: '1Gi'
        }
      }
    ]
    enableTelemetry: false
    environmentResourceId: containerAppsEnvironment.outputs.resourceId
    ingressAllowInsecure: false
    ingressExternal: true
    ingressTargetPort: 3000
    location: location
    managedIdentities: {
      userAssignedResourceIds: [
        webIdentity.id
      ]
    }
    registries: [
      {
        identity: webIdentity.id
        server: containerRegistry.outputs.loginServer
      }
    ]
    name: containerAppName
    scaleSettings: {
      maxReplicas: 2
      minReplicas: 0
    }
    tags: union(tags, {
      'azd-service-name': 'web'
    })
  }
}

output containerAppName string = containerApp.outputs.name
output containerAppUri string = 'https://${containerApp.outputs.fqdn}'
output containerAppsEnvironmentName string = containerAppsEnvironment.outputs.name
output containerRegistryLoginServer string = containerRegistry.outputs.loginServer
output containerRegistryName string = containerRegistry.outputs.name
output webClientId string = webIdentity.properties.clientId
output webPrincipalId string = webIdentity.properties.principalId
