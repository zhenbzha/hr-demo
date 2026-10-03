targetScope = 'resourceGroup'

param location string
param name string
param tags object = {}

module documentIntelligence 'br/public:avm/res/cognitive-services/account:0.19.1' = {
  params: {
    customSubDomainName: name
    disableLocalAuth: true
    enableTelemetry: false
    kind: 'FormRecognizer'
    location: location
    managedIdentities: {
      systemAssigned: true
    }
    name: name
    publicNetworkAccess: 'Enabled'
    restrictOutboundNetworkAccess: false
    sku: 'S0'
    tags: tags
  }
}

output endpoint string = documentIntelligence.outputs.endpoint
output id string = documentIntelligence.outputs.resourceId
output name string = documentIntelligence.outputs.name
