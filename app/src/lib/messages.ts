// the room log's bookkeeping, kept pure so it can be tested without a dom.
//
// a room's rendered log is persisted history (numeric db ids) plus lines
// that arrived live since the last fetch (uuid payload ids) plus the user's
// own optimistic sends. the same live line can reach us twice - once in the
// POST response and once over the websocket - so live appends dedupe by
// payload id. after a history refetch (mount, reconnect) the live/pending
// extras that history now contains are dropped by (author, content) match:
// history rows don't carry payload uuids, so identity can't bridge the two
// worlds - content matching is the honest seam.

import type { DeliveryPayload, HistoryRow } from "../api/types";

export interface ChatMessage {
  /** stable render key: `h{db id}` for history, payload uuid for live */
  key: string;
  author: string;
  authorType: "user" | "agent";
  content: string;
  at: string | null;
  /** refusal/error copy: rendered, never persisted, never deduped against */
  ephemeral?: boolean;
  /** an optimistic user line whose turn hasn't finished yet */
  pending?: boolean;
  /** the send didn't come back - retry MUST reuse clientMessageId, so an
   * ambiguous failure (server finished, response lost) replays the receipt
   * instead of running a second turn */
  failed?: boolean;
  /** the receipt key this line was sent under (optimistic lines only) */
  clientMessageId?: string;
}

export function fromHistory(row: HistoryRow): ChatMessage {
  return {
    key: `h${row.id}`,
    author: row.author,
    authorType: row.author_type === "user" ? "user" : "agent",
    content: row.content,
    at: row.at,
  };
}

let fallbackCounter = 0;
const fallbackKey = () => `e${++fallbackCounter}`;

export function fromPayload(payload: DeliveryPayload): ChatMessage {
  return {
    key: payload.id ?? fallbackKey(),
    author: payload.author,
    authorType: "agent",
    content: payload.content,
    at: payload.at ?? null,
    ephemeral: payload.ephemeral,
  };
}

/** which room a view accepts lines for (phase 6b: cycle rooms share the
 * one per-user socket with the daily room, so views filter by the
 * payload's room field). `strict` views (cycle rooms) drop lines that
 * don't name their room; the daily view also admits room-less payloads -
 * the pre-6b shape only ever carried daily lines. */
export interface RoomFilter {
  /** the viewing room's uuid; null while it hasn't loaded yet */
  room: string | null;
  strict?: boolean;
}

export function inRoom(
  payload: DeliveryPayload,
  filter: RoomFilter,
): boolean {
  if (payload.room) return payload.room === filter.room;
  return !filter.strict;
}

/**
 * admit a live payload unless it belongs to another room or its id was
 * already seen (POST response vs websocket double-delivery): returns the
 * message to append, or null. a foreign-room line is rejected BEFORE the
 * seen-set - it never burns the id another view may legitimately admit.
 * deliberately NOT shaped as a react state updater - it mutates `seen`
 * (and mints fallback keys), so it must run exactly once per event.
 * strict mode double-invokes updaters to catch exactly that impurity, and
 * did: v1 of this helper ran inside setState and dropped every line.
 */
export function admitLive(
  seen: Set<string>,
  payload: DeliveryPayload,
  filter?: RoomFilter,
): ChatMessage | null {
  if (filter && !inRoom(payload, filter)) return null;
  if (payload.id) {
    if (seen.has(payload.id)) return null;
    seen.add(payload.id);
  }
  return fromPayload(payload);
}

/**
 * after a history refetch: drop the extras history now covers. ephemeral
 * lines are never persisted, so they always survive; anything else survives
 * only while history doesn't contain the same (author, content) line.
 */
export function reconcileExtras(
  extras: ChatMessage[],
  history: ChatMessage[],
): ChatMessage[] {
  const covered = new Set(history.map((m) => `${m.author}\u0000${m.content}`));
  return extras.filter(
    (m) => m.ephemeral || !covered.has(`${m.author}\u0000${m.content}`),
  );
}

/** the user's optimistic line for a send that just left the composer */
export function pendingUserLine(clientMessageId: string, text: string): ChatMessage {
  return {
    key: `p${clientMessageId}`,
    author: "user",
    authorType: "user",
    content: text,
    at: null,
    pending: true,
    clientMessageId,
  };
}
