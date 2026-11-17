targetScope = 'resourceGroup'

param storageAccountName string

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource configurationLock 'Microsoft.Authorization/locks@2020-05-01' = {
  name: 'keep-demo-storage-public'
  scope: storageAccount
  properties: {
    level: 'ReadOnly'
    notes: 'Protect demo public networking and its exception tag. Remove for account configuration or RBAC maintenance.'
  }
}
