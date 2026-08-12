import { describe, expect, it } from "vitest";
import { STIRRINGS, pickStirring } from "./stirrings";

describe("pickStirring", () => {
  it("always returns a known phrase", () => {
    for (let i = 0; i < 50; i++) {
      expect(STIRRINGS).toContain(pickStirring());
    }
  });

  it("never repeats the current phrase (the rotation must visibly move)", () => {
    let current = pickStirring();
    for (let i = 0; i < 200; i++) {
      const next = pickStirring(current);
      expect(next).not.toBe(current);
      current = next;
    }
  });
});
