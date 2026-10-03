targetScope = 'resourceGroup'

param location string
param name string
param tags object = {}

module keyVault 'br/public:avm/res/key-vault/vault:0.14.2' = {
  params: {
    enablePurgeProtection: false
    enableRbacAuthorization: true
    enableTelemetry: false
    location: location
    name: name
    publicNetworkAccess: 'Disabled'
    softDeleteRetentionInDays: 7
    tags: tags
  }
}

output id string = keyVault.outputs.resourceId
output name string = keyVault.outputs.name
output uri string = keyVault.outputs.uri
