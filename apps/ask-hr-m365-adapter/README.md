# Ask HR Microsoft 365 adapter

Thin Microsoft 365 Agents SDK service that connects Teams to the existing Ask
HR Microsoft Foundry hosted agent.

## Request flow

```text
Signed-in Teams user
  -> Azure Bot Service
  -> Bot Framework JWT validation
  -> Microsoft 365 Agents SDK authorization handler
  -> Entra OBO token for Azure AI Search
  -> Ask HR Foundry Responses endpoint
  -> Foundry IQ / Azure AI Search
```

The adapter sends three separate identity values:

```http
Authorization: Bearer <adapter Foundry workload token>
x-client-search-authorization: <delegated Search user token>
x-ms-user-identity: <verified Entra object ID>
```

The object ID provides trusted per-user isolation in Foundry. It does not grant
Search access. The hosted agent requires the delegated Search token on every
request and forwards it to Search as `x-ms-query-source-authorization`.

## Security behavior

- `authorizeJWT` validates the Bot Framework token before the adapter sends any
  Teams activity.
- Channel authentication uses the adapter managed identity and federated
  credentials.
- Microsoft 365 Agents SDK 1.9.1 still requires a confidential-client
  credential for JavaScript OBO. That credential is stored only as an
  encrypted Container App secret.
- The adapter identity receives Foundry Agent Consumer, AcrPull, and the custom
  Foundry user-impersonation data action.
- Missing trusted user identity, missing delegated Search token, OBO failure,
  Foundry rejection, and request timeout fail closed.
- Tokens remain request-scoped and are never placed in prompts, Responses
  request bodies, conversation state, telemetry, logs, errors, or Teams
  messages.

## Responses contract

```json
{
  "input": "<question>",
  "stream": false,
  "background": false,
  "store": true,
  "previous_response_id": "<previous ID when available>"
}
```

Only the returned response ID is stored in conversation state.

## Teams delivery

- `longRunningMessages: true` releases the inbound Bot Service request.
- One best-effort status activity is sent before OBO and Foundry processing.
- The final answer replaces the status activity.
- If status delivery fails, processing continues.
- If status replacement fails, the final answer is sent as a new activity.
- A custom global error handler prevents SDK developer-facing messages from
  reaching users.
- `membersAdded` ignores the bot itself.

## Local validation

```powershell
pnpm install
pnpm typecheck
pnpm lint
pnpm test
pnpm build
pnpm audit --prod
```

The complete Teams SSO flow requires a deployed Azure Bot and can't be
validated through a local mock channel.

## Current Search ACL limitation

The current Ask HR index doesn't contain permission-filter metadata. The
delegated Search token is forwarded now, and missing tokens fail closed, but
different users will receive the same HR corpus until native ACL/RBAC metadata
and permission filters are added to the index.
