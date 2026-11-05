targetScope = 'resourceGroup'

param location string
param name string
param tags object = {}

var fileSystemName = 'hr-content'

module storage 'br/public:avm/res/storage/storage-account:0.33.1' = {
  params: {
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    enableHierarchicalNamespace: true
    blobServices: {
      containerDeleteRetentionPolicyDays: 7
      containerDeleteRetentionPolicyEnabled: true
      containers: [
        {
          name: fileSystemName
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
    // Demo-only opt-out supported by MCAPSGov StorageAccount_PublicNetwork_Modify.
    // Keep this account-scoped; a resource-group tag would exempt other resources.
    tags: union(tags, {
      SecurityControl: 'Ignore'
    })
  }
}

output blobEndpoint string = storage.outputs.primaryBlobEndpoint
output dfsEndpoint string = 'https://${storage.outputs.name}.dfs.${environment().suffixes.storage}/'
output fileSystemName string = fileSystemName
output id string = storage.outputs.resourceId
output name string = storage.outputs.name
