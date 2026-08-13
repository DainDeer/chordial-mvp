// the cycle panel's arithmetic: fills clamp, capacity lines stay honest,
// released promises stay out of the daily view.

import { describe, expect, it } from "vitest";
import type { CommitmentRow } from "../api/types";
import {
  capacityFill,
  capacityLine,
  commitmentFill,
  visibleCommitments,
} from "./cycle";

function row(overrides: Partial<CommitmentRow>): CommitmentRow {
  return {
    id: 1,
    public_id: "cm1",
    uuid: "u-1",
    title: "a promise",
    status: "active",
    priority: null,
    blocks_planned: null,
    baseline_blocks: null,
    blocks_done: 0,
    seconds_done: 0,
    next_action: null,
    plan_id: null,
    plan_title: null,
    task_id: null,
    task_title: null,
    ...overrides,
  };
}

describe("commitmentFill", () => {
  it("fills against the plan", () => {
    expect(commitmentFill(row({ blocks_planned: 4, blocks_done: 1 }))).toBe(
      0.25,
    );
  });

  it("caps at a full bar when the work overshoots the plan", () => {
    expect(commitmentFill(row({ blocks_planned: 2, blocks_done: 5 }))).toBe(1);
  });

  it("shows a sliver of life with no plan at all", () => {
    expect(
      commitmentFill(row({ blocks_planned: null, blocks_done: 0.3 })),
    ).toBeCloseTo(0.3);
  });
});

describe("capacityFill", () => {
  it("scales done and planned against capacity, planned never under done", () => {
    const bar = capacityFill({
      capacity_blocks: 20,
      planned_blocks: 10,
      done_blocks: 12,
      unattributed_seconds: 0,
      unallocated_blocks: 10,
    });
    expect(bar.done).toBeCloseTo(0.6);
    expect(bar.planned).toBeCloseTo(0.6); // raised to done, not left behind
  });

  it("survives a cycle with no declared capacity", () => {
    const bar = capacityFill({
      capacity_blocks: null,
      planned_blocks: 4,
      done_blocks: 2,
      unattributed_seconds: 0,
      unallocated_blocks: null,
    });
    expect(bar.planned).toBe(1);
    expect(bar.done).toBeCloseTo(0.5);
  });
});

describe("capacityLine", () => {
  it("tells the whole story when capacity is declared", () => {
    expect(
      capacityLine({
        capacity_blocks: 24,
        planned_blocks: 10,
        done_blocks: 2.3,
        unattributed_seconds: 0,
        unallocated_blocks: 14,
      }),
    ).toBe("10 of 24 blocks planned · 14 unallocated · 2.3 done");
  });

  it("stays quiet about what isn't set", () => {
    expect(
      capacityLine({
        capacity_blocks: null,
        planned_blocks: 4,
        done_blocks: 0,
        unattributed_seconds: 0,
        unallocated_blocks: null,
      }),
    ).toBe("4 blocks planned · 0 done");
  });
});

describe("visibleCommitments", () => {
  it("keeps active and completed, hides released", () => {
    const rows = [
      row({ uuid: "a", status: "active" }),
      row({ uuid: "b", status: "completed" }),
      row({ uuid: "c", status: "released" }),
    ];
    expect(visibleCommitments(rows).map((r) => r.uuid)).toEqual(["a", "b"]);
  });
});
