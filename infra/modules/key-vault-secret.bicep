targetScope = 'resourceGroup'

param secretName string
@secure()
param secretValue string
param vaultName string

module secret 'br/public:avm/res/key-vault/vault/secret:0.1.1' = {
  params: {
    enableTelemetry: false
    keyVaultName: vaultName
    name: secretName
    value: secretValue
  }
}

output secretUri string = secret.outputs.secretUri
