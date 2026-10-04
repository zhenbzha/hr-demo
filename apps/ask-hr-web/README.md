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
Ask HR Entra, Foundry, and ADLS settings. Generate `NEXTAUTH_SECRET` from at
least 32 cryptographically random bytes and keep it separate from
`ENTRA_CLIENT_SECRET`.

Sign in to Azure CLI for local Foundry and ADLS access, then run:

```powershell
az login
pnpm dev
```

Open [http://localhost:3000](http://localhost:3000) with your browser to see the result.
Port 3000 is fixed because it must match the registered Entra callback URL.
