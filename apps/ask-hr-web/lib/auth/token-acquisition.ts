import {
  type AccountInfo,
  type AuthenticationResult,
  InteractionRequiredAuthError,
} from "@azure/msal-node";

import type { UserSession } from "@/lib/auth/session";

export class ReauthenticationRequiredError extends Error {}

export interface SilentTokenClient {
  getAccountByHomeId(homeAccountId: string): Promise<AccountInfo | null>;
  acquireTokenSilent(request: {
    account: AccountInfo;
    scopes: string[];
  }): Promise<AuthenticationResult>;
}

export async function acquireSearchTokenWithClient(
  session: UserSession,
  scopes: string[],
  client: SilentTokenClient,
): Promise<string> {
  if (!session.account) {
    throw new ReauthenticationRequiredError(
      "User session is not authenticated",
    );
  }
  const account = await client.getAccountByHomeId(
    session.account.homeAccountId,
  );
  if (!account) {
    throw new ReauthenticationRequiredError(
      "The signed-in account is no longer present in the MSAL cache",
    );
  }

  try {
    const result = await client.acquireTokenSilent({
      account,
      scopes,
    });
    if (!result.accessToken) {
      throw new ReauthenticationRequiredError(
        "Microsoft Entra did not return a Search access token",
      );
    }
    return result.accessToken;
  } catch (error) {
    if (error instanceof InteractionRequiredAuthError) {
      throw new ReauthenticationRequiredError(
        "Microsoft Entra requires the user to sign in again",
      );
    }
    throw error;
  }
}
