import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  type AccountInfo,
  type AuthenticationResult,
  InteractionRequiredAuthError,
} from "@azure/msal-node";

import {
  acquireSearchTokenWithClient,
  ReauthenticationRequiredError,
  type SilentTokenClient,
} from "@/lib/auth/token-acquisition";

const account = {
  homeAccountId: "home-account",
  environment: "login.microsoftonline.com",
  tenantId: "tenant",
  username: "user@example.test",
  localAccountId: "user-object-id",
} as AccountInfo;

const session = {
  id: "session-id",
  updatedAt: Date.now(),
  account,
};

describe("MSAL Search token acquisition", () => {
  it("uses the signed-in account and requested Search scope", async () => {
    let capturedAccount: AccountInfo | undefined;
    let capturedScopes: string[] | undefined;
    const client: SilentTokenClient = {
      getAccountByHomeId: async () => account,
      acquireTokenSilent: async (request) => {
        capturedAccount = request.account;
        capturedScopes = request.scopes;
        return {
          accessToken: "search-token",
        } as AuthenticationResult;
      },
    };

    const token = await acquireSearchTokenWithClient(
      session,
      ["https://search.azure.com/.default"],
      client,
    );

    assert.equal(token, "search-token");
    assert.equal(capturedAccount, account);
    assert.deepEqual(capturedScopes, ["https://search.azure.com/.default"]);
  });

  it("fails closed when the account is absent from the cache", async () => {
    const client: SilentTokenClient = {
      getAccountByHomeId: async () => null,
      acquireTokenSilent: async () => {
        throw new Error("Should not run");
      },
    };
    await assert.rejects(
      acquireSearchTokenWithClient(session, ["scope"], client),
      ReauthenticationRequiredError,
    );
  });

  it("requires interactive sign-in when MSAL requests interaction", async () => {
    const client: SilentTokenClient = {
      getAccountByHomeId: async () => account,
      acquireTokenSilent: async () => {
        throw new InteractionRequiredAuthError(
          "interaction_required",
          "Interaction required",
        );
      },
    };
    await assert.rejects(
      acquireSearchTokenWithClient(session, ["scope"], client),
      /requires the user to sign in again/,
    );
  });
});
