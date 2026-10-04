import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { authErrorMessage } from "@/lib/auth/auth-errors";
import { safeReturnTo } from "@/lib/auth/safe-return-to";

describe("safeReturnTo", () => {
  const baseUrl = "https://ask-hr.example";

  it("accepts same-origin paths with query and fragment", () => {
    assert.equal(
      safeReturnTo("/chat?topic=leave#answer", baseUrl),
      "/chat?topic=leave#answer",
    );
  });

  it("rejects protocol-relative and backslash-based redirects", () => {
    assert.equal(safeReturnTo("//evil.example", baseUrl), "/");
    assert.equal(safeReturnTo("/\\evil.example", baseUrl), "/");
  });

  it("rejects absolute cross-origin URLs", () => {
    assert.equal(safeReturnTo("https://evil.example/", baseUrl), "/");
  });
});

describe("authErrorMessage", () => {
  it("maps only server-owned error codes", () => {
    assert.equal(
      authErrorMessage("session_expired"),
      "The sign-in session expired. Please try again.",
    );
    assert.equal(authErrorMessage("Fake sign-in failure"), undefined);
  });
});
