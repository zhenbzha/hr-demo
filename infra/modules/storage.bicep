targetScope = 'resourceGroup'

param location string
param name string
param tags object = {}

var manifestContainerName = 'manifests'
var sessionContainerName = 'sessions'
var sourceContainerName = 'hr-source'

module storage 'br/public:avm/res/storage/storage-account:0.33.1' = {
  params: {
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    blobServices: {
      containerDeleteRetentionPolicyDays: 7
      containerDeleteRetentionPolicyEnabled: true
      containers: [
        {
          name: sourceContainerName
          publicAccess: 'None'
        }
        {
          name: manifestContainerName
          publicAccess: 'None'
        }
        {
          name: sessionContainerName
          publicAccess: 'None'
        }
      ]
      deleteRetentionPolicyDays: 7
      deleteRetentionPolicyEnabled: true
    }
    enableTelemetry: false
    location: location
    name: name
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Allow'
      ipRules: []
      virtualNetworkRules: []
    }
    publicNetworkAccess: 'Enabled'
    requireInfrastructureEncryption: true
    skuName: 'Standard_LRS'
    tags: tags
  }
}

output blobEndpoint string = storage.outputs.primaryBlobEndpoint
output id string = storage.outputs.resourceId
output manifestContainerName string = manifestContainerName
output name string = storage.outputs.name
output sessionContainerName string = sessionContainerName
output sourceContainerName string = sourceContainerName
