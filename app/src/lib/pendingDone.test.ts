import { describe, expect, it } from "vitest";
import {
  addPendingDone,
  listPendingDone,
  removePendingDone,
} from "./pendingDone";

function fakeStorage(): Storage {
  const map = new Map<string, string>();
  return {
    getItem: (k) => map.get(k) ?? null,
    setItem: (k, v) => void map.set(k, v),
    removeItem: (k) => void map.delete(k),
    clear: () => map.clear(),
    key: () => null,
    get length() {
      return map.size;
    },
  } as Storage;
}

describe("the offline-finish ledger", () => {
  it("persists, dedupes, and removes", () => {
    const s = fakeStorage();
    expect(listPendingDone(s)).toEqual([]);
    addPendingDone(s, 7);
    addPendingDone(s, 9);
    addPendingDone(s, 7); // already there
    expect(listPendingDone(s)).toEqual([7, 9]);
    removePendingDone(s, 7);
    expect(listPendingDone(s)).toEqual([9]);
  });

  it("shrugs at corrupt storage", () => {
    const s = fakeStorage();
    s.setItem("chordial.pending_done", "{not json");
    expect(listPendingDone(s)).toEqual([]);
    s.setItem("chordial.pending_done", '["a", 3, null]');
    expect(listPendingDone(s)).toEqual([3]);
  });
});
