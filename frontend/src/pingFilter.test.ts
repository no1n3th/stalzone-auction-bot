import { describe, expect, it } from "vitest";
import { isPing } from "./lib/ping"; // F-10: test the real implementation

describe("ws ping filter", () => {
  it("drops pings", () => {
    expect(isPing({ type: "ping" })).toBe(true);
  });
  it("keeps events", () => {
    expect(isPing({ type: "deal" })).toBe(false);
    expect(isPing({ lot_sig: "a" })).toBe(false);
    expect(isPing("ping")).toBe(false);
  });
});
