import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { referencePaths } from "@/lib/references/paths";

describe("referencePaths", () => {
  it("uses a plain GUID and fingerprint for both citation artifacts", () => {
    const id = "6d18bb8e-1078-54ee-b224-301b9a17716a";
    const version = "a".repeat(32);
    assert.deepEqual(referencePaths(id, version), {
      documentPath: `documents/${id}/${version}.pdf`,
      annotationPath: `annotations/${id}/${version}.json`,
    });
  });

  it("keeps historical citations readable", () => {
    assert.deepEqual(referencePaths("HR-FI-003", "1.0"), {
      documentPath: "documents/HR-FI-003/1.0.pdf",
      annotationPath: "annotations/HR-FI-003/1.0.json",
    });
  });

  it("rejects path traversal, separators, and oversized identifiers", () => {
    for (const id of [
      "../other",
      "a/b",
      "a\\b",
      "%2e%2e",
      "",
      "a".repeat(65),
    ]) {
      assert.throws(() => referencePaths(id, "1.0"), /Invalid document ID/);
    }
    for (const version of ["../other", "a/b", "a\\b", "", "a".repeat(33)]) {
      assert.throws(
        () => referencePaths("HR-FI-003", version),
        /Invalid document version/,
      );
    }
  });
});
