# Ask HR web application

Next.js chat for the Ask HR hosted agent, with streaming answers, source cards,
and an embedded PDF viewer that highlights cited passages.

This guide describes the **authenticated application on `main`**. The deployed
no-login demo uses a separate branch; see the [project overview](../../README.md#try-it).

## Local development

Use Node.js 24, pnpm, and Azure CLI. From the repository root:

```powershell
Set-Location apps\ask-hr-web
pnpm install --frozen-lockfile
```

If `.env.local` does not already exist, copy `.env.example` to it. Configure the
existing Entra application, Foundry endpoint, and ADLS settings; do not overwrite
an existing local configuration. The file is gitignored.

Sign Azure CLI into the configured tenant with access to Foundry and ADLS, then
start the application from this directory:

```powershell
pnpm dev
```

Open [http://localhost:3000](http://localhost:3000). Port 3000 and the Entra
callback URL must match the registered application.

## Authentication

- The Entra application requires Azure Cognitive Search delegated
  `user_impersonation` permission with tenant consent.
- MSAL owns the access/refresh-token cache. The browser receives only an opaque
  session cookie, not Azure tokens.
- Each turn acquires the signed-in user's Search token and sends it separately
  from the backend's Foundry workload token.
- Conversation continuation uses the returned Foundry response ID.

The session and MSAL caches are process-local. The authenticated demo's
Container App is intentionally limited to one replica.
See the [hosted-agent authorization contract](../../ask-hr/README.md#authentication-and-retrieval).

## Local checks

```powershell
pnpm typecheck
pnpm lint
pnpm test
```

## Container deployment

The following commands **update the existing Azure deployment**. Run them from
the repository root with Docker running and `AZURE_SUBSCRIPTION_ID` set to the
intended subscription. Build the current working tree and push that same image:

```powershell
$tag = "dev-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
$image = "craskhr3yw342ef3vpq2.azurecr.io/ask-hr-web:$tag"

docker build --tag $image apps\ask-hr-web
az acr login --name craskhr3yw342ef3vpq2 --subscription $env:AZURE_SUBSCRIPTION_ID
docker push $image

az containerapp update `
  --subscription $env:AZURE_SUBSCRIPTION_ID `
  --resource-group rg-ask-hr-demo `
  --name ca-ask-hr-web `
  --image $image `
  --revision-suffix $tag
```

An image update does not provision resources or synchronize environment settings.
Keep the web configuration consistent with the selected branch and hosted agent.
