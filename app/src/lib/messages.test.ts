// the room log's bookkeeping is where cross-surface delivery gets subtle
// (the same line arrives via POST response AND websocket; history refetches
// overlap the live tail) - so it stays pure and gets pinned here.

import { describe, expect, it } from "vitest";
import type { DeliveryPayload, HistoryRow } from "../api/types";
import {
  admitLive,
  fromHistory,
  fromPayload,
  pendingUserLine,
  reconcileExtras,
} from "./messages";

const row = (id: number, author: string, content: string): HistoryRow => ({
  id,
  author,
  author_type: author === "user" ? "user" : "agent",
  content,
  at: "2026-08-12T10:00:00+00:00",
  platform: "app",
});

const payload = (
  id: string | undefined,
  author: string,
  content: string,
  ephemeral = false,
): DeliveryPayload => ({
  type: "message",
  id,
  author,
  content,
  at: "2026-08-12T10:00:05+00:00",
  ephemeral: ephemeral || undefined,
});

describe("fromHistory", () => {
  it("maps author_type to the render side and keys by db id", () => {
    const mine = fromHistory(row(3, "user", "hi"));
    expect(mine).toMatchObject({ key: "h3", authorType: "user" });
    const theirs = fromHistory(row(4, "vel", "*ears perk*"));
    expect(theirs).toMatchObject({ key: "h4", authorType: "agent" });
  });

  it("carries platform provenance for the tether marker (7a)", () => {
    const viaPhone = fromHistory({ ...row(5, "user", "from the bus"),
                                   platform: "telegram" });
    expect(viaPhone.platform).toBe("telegram");
  });
});

describe("fromPayload (the tether mirror, 7a)", () => {
  it("renders a mirrored user line on the person's side of the room", () => {
    const mirrored = fromPayload({
      ...payload("mid-1", "user", "logging lunch"),
      author_type: "user",
      platform: "telegram",
    });
    expect(mirrored).toMatchObject({
      authorType: "user",
      platform: "telegram",
      content: "logging lunch",
    });
  });

  it("keeps ordinary payloads as council lines", () => {
    const line = fromPayload(payload("mid-2", "remy", "~650, logged"));
    expect(line.authorType).toBe("agent");
    expect(line.platform).toBeNull();
  });

  it("mirrored lines reconcile against their history rows", () => {
    // after a refetch, the mirror's live echo and the persisted row are the
    // same (author, content) pair - the extra must drop, not double-render
    const extras = [
      fromPayload({ ...payload("mid-3", "user", "from the bus"),
                    author_type: "user", platform: "telegram" }),
    ];
    const history = [
      fromHistory({ ...row(6, "user", "from the bus"),
                    platform: "telegram" }),
    ];
    expect(reconcileExtras(extras, history)).toEqual([]);
  });
});

describe("admitLive", () => {
  it("admits a fresh payload and records its id", () => {
    const seen = new Set<string>();
    const line = admitLive(seen, payload("aaa", "pip", "one block!!"));
    expect(line).toMatchObject({ key: "aaa", author: "pip" });
    expect(seen.has("aaa")).toBe(true);
  });

  it("rejects the websocket copy of a line the POST response already carried", () => {
    const seen = new Set<string>();
    admitLive(seen, payload("aaa", "pip", "one block!!"));
    expect(admitLive(seen, payload("aaa", "pip", "one block!!"))).toBeNull();
  });

  it("never rejects id-less lines (ephemeral fallback copy has no payload id)", () => {
    const seen = new Set<string>();
    const first = admitLive(seen, payload(undefined, "vel", "hm.", true));
    const second = admitLive(seen, payload(undefined, "vel", "hm.", true));
    expect(first).not.toBeNull();
    expect(second).not.toBeNull();
    expect(first!.key).not.toBe(second!.key);
  });
});

describe("the room filter (phase 6b: cycle rooms share the socket)", () => {
  const inRoomPayload = (id: string, room?: string): DeliveryPayload => ({
    ...payload(id, "edwin", "*adjusts spectacles*"),
    room,
  });

  it("keeps another room's line out of a view", () => {
    const seen = new Set<string>();
    const line = admitLive(seen, inRoomPayload("aaa", "retro-room"), {
      room: "daily-room",
    });
    expect(line).toBeNull();
  });

  it("does not burn the id when rejecting by room", () => {
    // the same payload reaches every view's handler; the room it belongs
    // to must still be able to admit it after another view rejected it
    const seen = new Set<string>();
    admitLive(seen, inRoomPayload("aaa", "retro-room"), {
      room: "daily-room",
    });
    expect(seen.has("aaa")).toBe(false);
    const line = admitLive(seen, inRoomPayload("aaa", "retro-room"), {
      room: "retro-room",
      strict: true,
    });
    expect(line).not.toBeNull();
  });

  it("the daily view admits room-less payloads (the pre-6b shape)", () => {
    const seen = new Set<string>();
    const line = admitLive(seen, inRoomPayload("aaa"), {
      room: "daily-room",
    });
    expect(line).not.toBeNull();
  });

  it("a strict (cycle) view drops room-less payloads", () => {
    const seen = new Set<string>();
    const line = admitLive(seen, inRoomPayload("aaa"), {
      room: "retro-room",
      strict: true,
    });
    expect(line).toBeNull();
  });

  it("a view that hasn't resolved its room yet only takes room-less lines", () => {
    const seen = new Set<string>();
    expect(
      admitLive(seen, inRoomPayload("aaa", "retro-room"), { room: null }),
    ).toBeNull();
    expect(
      admitLive(seen, inRoomPayload("bbb"), { room: null }),
    ).not.toBeNull();
  });
});

describe("reconcileExtras", () => {
  it("drops live lines a history refetch now covers", () => {
    const extras = [
      fromPayload(payload("aaa", "pip", "one block!!")),
      fromPayload(payload("bbb", "mabel", "water first.")),
    ];
    const history = [
      fromHistory(row(1, "user", "starting now")),
      fromHistory(row(2, "pip", "one block!!")),
    ];
    const survivors = reconcileExtras(extras, history);
    expect(survivors.map((m) => m.author)).toEqual(["mabel"]);
  });

  it("drops a confirmed optimistic user line once history holds it", () => {
    const mine = { ...pendingUserLine("cmid-1", "starting now"), pending: false };
    const history = [fromHistory(row(1, "user", "starting now"))];
    expect(reconcileExtras([mine], history)).toEqual([]);
  });

  it("keeps ephemeral lines - they are never persisted, so history never covers them", () => {
    const whisper = fromPayload(payload(undefined, "vel", "hm.", true));
    const history = [fromHistory(row(9, "vel", "hm."))];
    expect(reconcileExtras([whisper], history)).toEqual([whisper]);
  });

  it("admits identical content under distinct payload ids", () => {
    // dedupe is id-based during live admission (content repeats are real:
    // "again!" twice is two lines) and content-based ONLY at refetch
    const seen = new Set<string>();
    expect(admitLive(seen, payload("aaa", "pip", "again!"))).not.toBeNull();
    expect(admitLive(seen, payload("bbb", "pip", "again!"))).not.toBeNull();
  });
});
