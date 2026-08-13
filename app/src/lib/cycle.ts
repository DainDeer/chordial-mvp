// pure helpers behind the cycle panel - kept out of the component so the
// arithmetic is testable without rendering.

import type { CommitmentRow, CyclePayload } from "../api/types";

/** a commitment's progress fill, 0..1. no planned blocks = fill by a single
 * block so any real minutes still show a sliver of life. capped at 1 -
 * overshooting the plan is a full bar, not a broken layout. */
export function commitmentFill(c: CommitmentRow): number {
  const planned = c.blocks_planned && c.blocks_planned > 0
    ? c.blocks_planned
    : 1;
  return Math.min(1, c.blocks_done / planned);
}

/** the cycle-level bar: how much of capacity is done / planned beyond done.
 * both 0..1 and clamped so a re-scoped cycle never overflows its track. */
export function capacityFill(totals: NonNullable<CyclePayload["totals"]>): {
  done: number;
  planned: number;
} {
  const capacity =
    totals.capacity_blocks && totals.capacity_blocks > 0
      ? totals.capacity_blocks
      : Math.max(totals.planned_blocks, totals.done_blocks, 1);
  const done = Math.min(1, totals.done_blocks / capacity);
  const planned = Math.min(1, totals.planned_blocks / capacity);
  return { done, planned: Math.max(planned, done) };
}

/** one honest sentence under the bar. */
export function capacityLine(
  totals: NonNullable<CyclePayload["totals"]>,
): string {
  const bits: string[] = [];
  if (totals.capacity_blocks != null) {
    bits.push(`${totals.planned_blocks} of ${totals.capacity_blocks} blocks planned`);
    if (totals.unallocated_blocks != null && totals.unallocated_blocks > 0) {
      bits.push(`${totals.unallocated_blocks} unallocated`);
    }
  } else {
    bits.push(`${totals.planned_blocks} blocks planned`);
  }
  bits.push(`${totals.done_blocks} done`);
  return bits.join(" · ");
}

/** commitments worth showing: open ones first (by id), released last-resort
 * hidden entirely - a dropped promise doesn't need a daily reminder. */
export function visibleCommitments(rows: CommitmentRow[]): CommitmentRow[] {
  return rows.filter((c) => c.status !== "released");
}
