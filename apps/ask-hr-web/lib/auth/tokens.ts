import "server-only";

import { msalClient } from "@/lib/auth/msal-client";
import type { UserSession } from "@/lib/auth/session";
import {
  acquireSearchTokenWithClient,
  ReauthenticationRequiredError,
} from "@/lib/auth/token-acquisition";
import { searchScopes } from "@/lib/config";

export { ReauthenticationRequiredError };

export function acquireSearchToken(session: UserSession): Promise<string> {
  return acquireSearchTokenWithClient(session, searchScopes, {
    getAccountByHomeId: (homeAccountId) =>
      msalClient.getTokenCache().getAccountByHomeId(homeAccountId),
    acquireTokenSilent: (request) => msalClient.acquireTokenSilent(request),
  });
}
