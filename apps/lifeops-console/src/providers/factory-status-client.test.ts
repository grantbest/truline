import { describe, expect, it } from "vitest";

import { FactoryStatusError, refusalMessage } from "./factory-status-client";

// Gate finding F3 (#920). `refusalMessage` is the one new behavioural
// function in this change and it is exactly what AC-1 requires -- "a refusal
// SHALL surface with the intake's own reason text, not a generic error" --
// yet it shipped with no coverage at all: FactoryBoard.test.tsx's tests never
// reach the filing mutation. A pure function implementing an acceptance
// criterion is the cheapest possible thing to assert, so it is asserted here.
//
// The cases are chosen to pin the PRECEDENCE, not just the happy path: each
// fallback must be reachable, and the intake's own text must beat every one
// of them. A test that only drove the 422 would pass even if the fallback
// chain were inverted.
describe("refusalMessage", () => {
  it("prefers the intake's own reason text from a FastAPI detail body", () => {
    const error = new FactoryStatusError(
      422,
      { detail: "requirement reference(s) resolve to nothing: PC-FAC-001/AC-5" },
      "Unprocessable Entity",
    );
    expect(refusalMessage(error, "fallback")).toBe(
      "requirement reference(s) resolve to nothing: PC-FAC-001/AC-5",
    );
  });

  it("does not let the HTTP status line win over the detail", () => {
    // The Error's own message is non-empty here, so this fails if the
    // fallback order is ever reversed.
    const error = new FactoryStatusError(503, { detail: "substrate unreachable" }, "Service Unavailable");
    expect(refusalMessage(error, "fallback")).toBe("substrate unreachable");
  });

  it("falls back to the error message when the body carries no usable detail", () => {
    for (const body of [null, {}, { detail: 42 }, { detail: "   " }, "not an object"]) {
      const error = new FactoryStatusError(500, body, "Internal Server Error");
      expect(refusalMessage(error, "fallback")).toBe("Internal Server Error");
    }
  });

  it("falls back to the error message for a plain Error, e.g. a network failure", () => {
    expect(refusalMessage(new Error("Failed to fetch"), "fallback")).toBe("Failed to fetch");
  });

  it("uses the caller's fallback only when nothing else says anything", () => {
    expect(refusalMessage(new Error(""), "could not file")).toBe("could not file");
    expect(refusalMessage(undefined, "could not file")).toBe("could not file");
    expect(refusalMessage({ detail: "not an Error at all" }, "could not file")).toBe("could not file");
  });
});
