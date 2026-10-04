targetScope = 'subscription'

param assignableScopeId string
param roleName string = 'Ask HR Foundry User Impersonation'

var roleDefinitionGuid = guid(
  subscription().id,
  'ask-hr-foundry-user-impersonation'
)

resource roleDefinition 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: roleDefinitionGuid
  properties: {
    roleName: roleName
    description: 'Allows trusted Ask HR services to delegate a verified end-user identity to the hosted agent endpoint.'
    type: 'CustomRole'
    assignableScopes: [
      assignableScopeId
    ]
    permissions: [
      {
        actions: []
        notActions: []
        dataActions: [
          'Microsoft.CognitiveServices/accounts/AIServices/agents/endpoints/UserIdentityImpersonation/action'
        ]
        notDataActions: []
      }
    ]
  }
}

output roleDefinitionId string = roleDefinition.id
