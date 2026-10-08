import { describe, expect, it } from "vitest";
import { SUBSTRATE_PROXY } from "@/lib/env";

describe("SUBSTRATE_PROXY", () => {
  it("defaults to the stable browser-facing substrate proxy path", () => {
    expect(SUBSTRATE_PROXY).toBe("/substrate");
  });
});
