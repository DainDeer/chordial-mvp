// the card's copy arithmetic: impact framing, neutral words, clamps.
import { describe, expect, it } from "vitest";
import type { RewindCandidate, RewindOffer } from "../api/sidecar";
import {
  altLabel,
  amountLabel,
  appliedLabel,
  chipLabel,
  clockLabel,
  frozenVerb,
  quietLine,
  removeLabel,
} from "./rewind";

const candidate = (removed: number, credited: number): RewindCandidate => ({
  at: "2026-08-17T14:14:00+00:00",
  kind: "run_end",
  rank: 0,
  removed_seconds: removed,
  credited_seconds: credited,
});

const offer = (contested: number): RewindOffer => ({
  offer_uuid: "o1",
  run_id: 1,
  frozen: false,
  contested_seconds: contested,
  candidates: [candidate(contested, 840)],
});

describe("amounts and clocks", () => {
  it("renders minutes past 60s and seconds under it", () => {
    expect(amountLabel(17 * 60)).toBe("17m");
    expect(amountLabel(45)).toBe("45s");
    expect(amountLabel(-5)).toBe("0s"); // never a negative amount
  });

  it("renders the clock as m:ss and clamps below zero", () => {
    expect(clockLabel(848)).toBe("14:08");
    expect(clockLabel(0)).toBe("0:00");
    expect(clockLabel(-30)).toBe("0:00");
  });
});

describe("the card's language is impact, never inference", () => {
  it("states the removal and the resulting clock", () => {
    expect(removeLabel(candidate(1020, 848))).toBe(
      "remove 17m · clock becomes 14:08",
    );
  });

  it("observes quiet without accusing", () => {
    const line = quietLine(offer(17 * 60));
    expect(line).toBe("the clock kept running through about 17m of quiet.");
    // the banned framings never appear
    for (const banned of ["wandered", "left", "distracted", "working?"]) {
      expect(line).not.toContain(banned);
    }
  });

  it("offers the alternative as a disclosure, gently", () => {
    expect(altLabel(candidate(11 * 60, 20 * 60 + 5))).toBe(
      "or remove 11m — if that blip was you",
    );
  });

  it("labels the chip as a review, not a debt", () => {
    expect(chipLabel(offer(17 * 60))).toBe("review 17m of quiet time");
  });

  it("shows the applied resting state with undo's numbers", () => {
    expect(appliedLabel(1020, 848)).toBe("17m removed · clock now 14:08");
  });
});

describe("frozen resolutions wear the transition's clothes", () => {
  it("maps reasons to verbs", () => {
    expect(frozenVerb("paused")).toBe("pause");
    expect(frozenVerb("finished")).toBe("finish");
    expect(frozenVerb("switched")).toBe("bank it");
    expect(frozenVerb(null)).toBe("pause");
  });
});
