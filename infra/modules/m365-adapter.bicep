targetScope = 'resourceGroup'

param applicationInsightsConnectionString string
param containerAppName string = 'ca-ask-hr-m365-adapter'
param containerAppsEnvironmentName string
param containerRegistryName string
param foundryAccountName string
param foundryAgentName string = 'ask-hr'
param foundryProjectName string
param impersonationRoleDefinitionId string
param location string
param serviceImageName string
param tags object = {}

var acrPullRoleDefinitionId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '7f951dda-4ed3-4680-a7ca-43fe172d538d'
)
var foundryAgentConsumerRoleDefinitionId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'eed3b665-ab3a-47b6-8f48-c9382fb1dad6'
)
var foundryAgentEndpoint = 'https://${foundryAccountName}.services.ai.azure.com/api/projects/${foundryProjectName}/agents/${foundryAgentName}/endpoint/protocols/openai/responses?api-version=v1'

resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: containerAppsEnvironmentName
}

resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: containerRegistryName
}

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2026-03-01' existing = {
  name: foundryAccountName
}

resource foundryProject 'Microsoft.CognitiveServices/accounts/projects@2026-03-01' existing = {
  parent: foundryAccount
  name: foundryProjectName
}

resource adapterIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-ask-hr-m365-adapter'
  location: location
  tags: tags
}

resource acrPullAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(containerRegistry.id, adapterIdentity.id, acrPullRoleDefinitionId)
  scope: containerRegistry
  properties: {
    principalId: adapterIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPullRoleDefinitionId
  }
}

resource foundryConsumerAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryProject.id, adapterIdentity.id, foundryAgentConsumerRoleDefinitionId)
  scope: foundryProject
  properties: {
    principalId: adapterIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: foundryAgentConsumerRoleDefinitionId
  }
}

resource foundryImpersonationAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryProject.id, adapterIdentity.id, impersonationRoleDefinitionId)
  scope: foundryProject
  properties: {
    principalId: adapterIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: impersonationRoleDefinitionId
  }
}

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: containerAppName
  location: location
  tags: union(tags, {
    'azd-service-name': 'ask-hr-m365-adapter'
  })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${adapterIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerAppsEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 3978
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: containerRegistry.properties.loginServer
          identity: adapterIdentity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'ask-hr-m365-adapter'
          image: serviceImageName
          env: [
            {
              name: 'NODE_ENV'
              value: 'production'
            }
            {
              name: 'PORT'
              value: '3978'
            }
            {
              name: 'AZURE_CLIENT_ID'
              value: adapterIdentity.properties.clientId
            }
            {
              name: 'FOUNDRY_AGENT_RESPONSES_ENDPOINT'
              value: foundryAgentEndpoint
            }
            {
              name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
              value: applicationInsightsConnectionString
            }
          ]
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
        rules: [
          {
            name: 'http'
            http: {
              metadata: {
                concurrentRequests: '20'
              }
            }
          }
        ]
      }
    }
  }
  dependsOn: [
    acrPullAssignment
    foundryConsumerAssignment
    foundryImpersonationAssignment
  ]
}

output containerAppFqdn string = containerApp.properties.configuration.ingress.fqdn
output containerAppName string = containerApp.name
output containerAppUri string = 'https://${containerApp.properties.configuration.ingress.fqdn}'
output foundryAgentResponsesEndpoint string = foundryAgentEndpoint
output managedIdentityClientId string = adapterIdentity.properties.clientId
output managedIdentityPrincipalId string = adapterIdentity.properties.principalId
output managedIdentityResourceId string = adapterIdentity.id
