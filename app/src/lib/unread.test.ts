// room-aware unread (sol's 7a P1): the daily door's badge counts exactly
// the daily-room council lines the person isn't watching - no more, no less.

import { describe, expect, it } from "vitest";
import type { DeliveryPayload } from "../api/types";
import { classifyForNudge } from "./unread";

const DAILY = "room-daily-1";
const CYCLE = "room-cycle-9";

const line = (room?: string, author_type?: string): DeliveryPayload => ({
  type: "message",
  id: "p1",
  author: author_type === "user" ? "user" : "remy",
  content: "words",
  room,
  author_type,
});

describe("classifyForNudge", () => {
  it("nudges for a daily line from home", () => {
    expect(classifyForNudge(line(DAILY), DAILY, "home")).toBe("nudge");
  });

  it("nudges for a daily line while a CYCLE room is open (sol's repro)", () => {
    // the line is filtered from the cycle view's transcript, so it was
    // never seen - "watched" must mean today's room specifically
    expect(classifyForNudge(line(DAILY), DAILY, "cycle")).toBe("nudge");
  });

  it("stays quiet while today's room is actually open", () => {
    expect(classifyForNudge(line(DAILY), DAILY, "room")).toBe("ignore");
    expect(classifyForNudge(line(undefined), DAILY, "room")).toBe("ignore");
  });

  it("never lets a cycle line inflate the daily door (sol's repro)", () => {
    // a delayed cycle reply arriving on home: unknown room -> resolve;
    // after the re-resolve it is still foreign -> ignore
    expect(classifyForNudge(line(CYCLE), DAILY, "home")).toBe("resolve");
    expect(classifyForNudge(line(CYCLE), DAILY, "home", true)).toBe("ignore");
  });

  it("re-resolves for a rolled-over daily room, then nudges", () => {
    const newDaily = "room-daily-2";
    // dailyRoom ref is stale (yesterday's): unknown -> resolve...
    expect(classifyForNudge(line(newDaily), DAILY, "home")).toBe("resolve");
    // ...and once the ref is refreshed, it's a daily line like any other
    expect(classifyForNudge(line(newDaily), newDaily, "home", true)).toBe(
      "nudge",
    );
  });

  it("room-less payloads are daily by contract (pre-6b shape)", () => {
    expect(classifyForNudge(line(undefined), DAILY, "home")).toBe("nudge");
    expect(classifyForNudge(line(undefined), null, "archive")).toBe("nudge");
  });

  it("the person's own mirrored phone lines never nudge", () => {
    expect(classifyForNudge(line(DAILY, "user"), DAILY, "home")).toBe(
      "ignore",
    );
  });

  it("holds for a resolve verdict while the daily room is still unknown", () => {
    expect(classifyForNudge(line(DAILY), null, "home")).toBe("resolve");
    expect(classifyForNudge(line(DAILY), DAILY, "home", true)).toBe("nudge");
  });
});
