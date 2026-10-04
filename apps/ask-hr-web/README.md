This is a [Next.js](https://nextjs.org) project bootstrapped with [`create-next-app`](https://nextjs.org/docs/app/api-reference/cli/create-next-app).

## Container build

Use the local Docker engine to build the current working tree, then push the
same tested image to the existing ACR:

```powershell
$tag = "dev-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
$image = "craskhr3yw342ef3vpq2.azurecr.io/ask-hr-web:$tag"

docker build --tag $image apps\ask-hr-web
az acr login --name craskhr3yw342ef3vpq2
docker push $image

az containerapp update `
  --resource-group rg-ask-hr-demo `
  --name ca-ask-hr-web `
  --image $image `
  --revision-suffix $tag
```

## Local development

Copy `.env.example` to the git-ignored `.env.local` and provide the existing
Ask HR Entra, Foundry, and ADLS settings.

The Entra application must have the Azure Cognitive Search delegated
`user_impersonation` permission with tenant consent. MSAL Node owns access and
refresh tokens in its process-local token cache; application code never reads
refresh tokens. The browser cookie contains only an opaque random session ID.
The Container App is intentionally limited to one replica for this POC cache
design.

Each chat turn uses `acquireTokenSilent` for the signed-in account and sends
the Search token to the hosted agent separately from the web app's Foundry
workload token.
Only the Foundry response ID is retained for conversation continuation.

Sign in to Azure CLI for local Foundry and ADLS access, then run:

```powershell
az login
pnpm dev
```

Open [http://localhost:3000](http://localhost:3000) with your browser to see the result.
Port 3000 is fixed because it must match the registered Entra callback URL.
