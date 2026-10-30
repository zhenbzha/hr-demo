targetScope = 'resourceGroup'

param accountName string
param classifierDeploymentName string
param classifierModelCapacity int = 10
param evaluationDeploymentName string
param evaluationModelCapacity int = 100
param generationDeploymentName string
param generationModelCapacity int = 100
param location string
param projectName string
param tags object = {}

var embeddingDeploymentName = 'ask-hr-text-embedding-3-large'

module account 'br/public:avm/res/cognitive-services/account:0.19.1' = {
  params: {
    allowProjectManagement: true
    customSubDomainName: accountName
    deployments: [
      {
        model: {
          format: 'OpenAI'
          name: 'gpt-4.1-mini'
          version: '2025-04-14'
        }
        name: classifierDeploymentName
        raiPolicyName: 'Microsoft.DefaultV2'
        sku: {
          capacity: classifierModelCapacity
          name: 'GlobalStandard'
        }
      }
      {
        model: {
          format: 'OpenAI'
          name: 'gpt-6.1-sol'
          version: '2026-09-29'
        }
        name: evaluationDeploymentName
        raiPolicyName: 'Microsoft.DefaultV2'
        sku: {
          capacity: evaluationModelCapacity
          name: 'GlobalStandard'
        }
      }
      {
        model: {
          format: 'OpenAI'
          name: 'gpt-6-astra'
          version: '2026-09-03'
        }
        name: generationDeploymentName
        raiPolicyName: 'Microsoft.DefaultV2'
        versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
        sku: {
          capacity: generationModelCapacity
          name: 'GlobalStandard'
        }
      }
      {
        model: {
          format: 'OpenAI'
          name: 'text-embedding-3-large'
          version: '1'
        }
        name: embeddingDeploymentName
        raiPolicyName: 'Microsoft.DefaultV2'
        sku: {
          capacity: 120
          name: 'GlobalStandard'
        }
      }
    ]
    disableLocalAuth: true
    enableTelemetry: false
    kind: 'AIServices'
    location: location
    managedIdentities: {
      systemAssigned: true
    }
    name: accountName
    publicNetworkAccess: 'Enabled'
    restrictOutboundNetworkAccess: false
    sku: 'S0'
    tags: tags
  }
}

resource accountResource 'Microsoft.CognitiveServices/accounts@2026-03-01' existing = {
  name: accountName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2026-03-01' = {
  parent: accountResource
  name: projectName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: 'Ask HR'
    description: 'Microsoft Foundry project for the Ask HR demo'
  }
  dependsOn: [
    account
  ]
}

output accountId string = account.outputs.resourceId
output accountName string = account.outputs.name
output accountPrincipalId string = account.outputs.?systemAssignedMIPrincipalId ?? ''
output classifierDeploymentName string = classifierDeploymentName
output embeddingDeploymentName string = embeddingDeploymentName
output evaluationDeploymentName string = evaluationDeploymentName
output generationDeploymentName string = generationDeploymentName
output openAiEndpoint string = 'https://${account.outputs.name}.openai.azure.com'
output projectEndpoint string = 'https://${account.outputs.name}.services.ai.azure.com/api/projects/${project.name}'
output projectId string = project.id
output projectName string = project.name
output projectPrincipalId string = project.identity.principalId
