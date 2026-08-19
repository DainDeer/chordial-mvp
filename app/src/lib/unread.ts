// the door's unread nudge, made room-aware (sol's 7a P1): the badge on
// "today's room" counts ONLY today's daily-room lines, and counts them
// whenever that room isn't the view being watched - including while the
// person sits in a cycle room. a cycle room's own lines never touch the
// daily door (they belong to a different door), and the person's mirrored
// phone lines never nudge (the reply that follows them will).
//
// kept pure so it can be tested without a dom.

import type { DeliveryPayload } from "../api/types";

export type NudgeVerdict =
  | "nudge" // a daily-room council line the person isn't watching
  | "ignore" // watched, foreign-room, or the person's own words
  | "resolve"; // unknown room id: today may have rolled over - re-resolve
//               the current daily room, then classify again with resolved=true

export function classifyForNudge(
  payload: DeliveryPayload,
  dailyRoom: string | null,
  view: string,
  resolved = false,
): NudgeVerdict {
  if (payload.author_type === "user") return "ignore";
  const watchingDaily = view === "room";
  if (!payload.room) {
    // room-less payloads are the pre-6b shape, which only ever carried
    // daily-room lines
    return watchingDaily ? "ignore" : "nudge";
  }
  if (dailyRoom !== null && payload.room === dailyRoom) {
    return watchingDaily ? "ignore" : "nudge";
  }
  // a room we don't recognize: either the day rolled over (a NEW daily
  // room) or it's a cycle room's line. one re-resolve tells them apart;
  // after that, a still-foreign room is someone else's door.
  return resolved ? "ignore" : "resolve";
}
