targetScope = 'resourceGroup'

param accountName string
param deploymentName string = 'ask-hr-gpt-5.4'
param modelName string = 'gpt-5.4'
param modelVersion string = '2026-03-05'
param capacity int = 100

resource account 'Microsoft.CognitiveServices/accounts@2026-03-01' existing = {
  name: accountName
}

resource deployment 'Microsoft.CognitiveServices/accounts/deployments@2026-03-01' = {
  parent: account
  name: deploymentName
  sku: {
    name: 'GlobalStandard'
    capacity: capacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: modelName
      version: modelVersion
    }
    raiPolicyName: 'Microsoft.DefaultV2'
  }
}

output deploymentName string = deployment.name
