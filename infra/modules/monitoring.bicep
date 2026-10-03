targetScope = 'resourceGroup'

param location string
param tags object = {}

module monitoring 'br/public:avm/ptn/azd/monitoring:0.2.1' = {
  params: {
    applicationInsightsName: 'appi-ask-hr-demo'
    enableTelemetry: false
    location: location
    logAnalyticsName: 'log-ask-hr-demo'
    tags: tags
  }
}

output applicationInsightsConnectionString string = monitoring.outputs.applicationInsightsConnectionString
output applicationInsightsName string = monitoring.outputs.applicationInsightsName
output logAnalyticsName string = monitoring.outputs.logAnalyticsWorkspaceName
output logAnalyticsResourceId string = monitoring.outputs.logAnalyticsWorkspaceResourceId
