// rewind's copy arithmetic, pure and testable (docs/REWIND_DESIGN.md §6).
// impact language, never inference: buttons say what they do to the clock,
// and no string here ever asks the person to defend how they spent time.

import type { RewindCandidate, RewindOffer } from "../api/sidecar";

/** "17m" / "45s" - short amount for buttons and the chip */
export function amountLabel(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  return `${Math.round(s / 60)}m`;
}

/** "14:08" - what the clock becomes */
export function clockLabel(totalSeconds: number): string {
  const s = Math.max(0, Math.round(totalSeconds));
  const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, "0")}`;
}

/** the card's observation line: neutral, an amount of quiet - never an
 * accusation ("you wandered", "you left" are banned by the design doc) */
export function quietLine(offer: RewindOffer): string {
  return `the clock kept running through about ${amountLabel(
    offer.contested_seconds,
  )} of quiet.`;
}

/** the primary button: "remove 17m · clock becomes 14:00". the caller
 * supplies creditedSeconds LIVE for an active offer (the local clock
 * keeps running while the question is deferred - a stale server value
 * would promise "14:00" and deliver "24:00"); a frozen offer's server
 * value is already exact. */
export function removeLabel(
  removedSeconds: number,
  creditedSeconds: number,
): string {
  return `remove ${amountLabel(removedSeconds)} · clock becomes ${clockLabel(
    creditedSeconds,
  )}`;
}

/** the rank-1 disclosure: "or remove 11m — if that blip was you" */
export function altLabel(candidate: RewindCandidate): string {
  return `or remove ${amountLabel(candidate.removed_seconds)} — if that blip was you`;
}

/** the collapsed chip: "review 17m of quiet time" */
export function chipLabel(offer: RewindOffer): string {
  return `review ${amountLabel(offer.contested_seconds)} of quiet time`;
}

/** what a resolution does to a FROZEN run: it banks it, wearing the
 * intended transition's clothes */
export function frozenVerb(reason: string | null | undefined): string {
  if (reason === "finished") return "finish";
  if (reason === "switched") return "bank it";
  return "pause";
}

/** the applied resting state: "17m removed · clock now 14:00" */
export function appliedLabel(
  removedSeconds: number,
  creditedSeconds: number,
): string {
  return `${amountLabel(removedSeconds)} removed · clock now ${clockLabel(
    creditedSeconds,
  )}`;
}
