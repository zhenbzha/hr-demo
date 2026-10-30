targetScope = 'resourceGroup'

param location string
param name string
param tags object = {}

module search 'br/public:avm/res/search/search-service:0.13.0' = {
  params: {
    disableLocalAuth: true
    enableTelemetry: false
    location: location
    managedIdentities: {
      systemAssigned: true
    }
    name: name
    partitionCount: 1
    publicNetworkAccess: 'Enabled'
    replicaCount: 1
    semanticSearch: 'standard'
    sku: 'basic'
    tags: tags
  }
}

output endpoint string = search.outputs.endpoint
output id string = search.outputs.resourceId
output name string = search.outputs.name
output principalId string = search.outputs.?systemAssignedMIPrincipalId ?? ''
