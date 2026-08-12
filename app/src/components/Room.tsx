import { useCallback, useEffect, useRef, useState } from "react";
import {
  fetchRoomCurrent,
  fetchRoomMessages,
  isAuthError,
  sendRoomMessage,
} from "../api/client";
import { RoomSocket, type SocketStatus } from "../api/ws";
import type { CouncilMember, RoomInfo } from "../api/types";
import {
  admitLive,
  fromHistory,
  pendingUserLine,
  reconcileExtras,
  type ChatMessage,
} from "../lib/messages";
import { memberHue } from "../lib/council";

interface Props {
  token: string;
  council: CouncilMember[];
  onTurnComplete: () => void;
  onAuthLost: () => void;
}

const STATUS_DOT: Record<SocketStatus, string> = {
  open: "live",
  connecting: "waking",
  closed: "away",
  revoked: "away",
};

/** today's room: the shared space where the council answers. history is the
 * persisted log; live lines arrive over the websocket and the send response
 * (deduped by payload id), and the composer's own line shows optimistically. */
export default function Room({
  token,
  council,
  onTurnComplete,
  onAuthLost,
}: Props) {
  const [room, setRoom] = useState<RoomInfo | null>(null);
  const [history, setHistory] = useState<ChatMessage[]>([]);
  const [extras, setExtras] = useState<ChatMessage[]>([]);
  const [socketStatus, setSocketStatus] = useState<SocketStatus>("connecting");
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);

  const seen = useRef<Set<string>>(new Set());
  const logRef = useRef<HTMLDivElement | null>(null);
  const pinnedToBottom = useRef(true);

  const refresh = useCallback(async () => {
    try {
      const [current, log] = await Promise.all([
        fetchRoomCurrent(token),
        fetchRoomMessages(token),
      ]);
      setRoom(current.room);
      const rows = log.messages.map(fromHistory);
      setHistory(rows);
      setExtras((prev) => reconcileExtras(prev, rows));
    } catch (e) {
      if (isAuthError(e)) onAuthLost();
    }
  }, [token, onAuthLost]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    const socket = new RoomSocket(token, {
      // dedupe OUTSIDE the state updater: updaters must stay pure (strict
      // mode double-invokes them), the seen-set mutation must run once
      onPayload: (payload) => {
        const line = admitLive(seen.current, payload);
        if (line) setExtras((prev) => [...prev, line]);
      },
      onStatus: (status) => {
        setSocketStatus(status);
        if (status === "revoked") onAuthLost();
      },
      onReconnect: refresh,
    });
    socket.start();
    return () => socket.stop();
  }, [token, refresh, onAuthLost]);

  const messages = [...history, ...extras];

  // stay pinned to the newest line unless the reader scrolled up on purpose
  useEffect(() => {
    const el = logRef.current;
    if (el && pinnedToBottom.current) el.scrollTop = el.scrollHeight;
  }, [messages.length, sending]);

  function onLogScroll() {
    const el = logRef.current;
    if (!el) return;
    pinnedToBottom.current =
      el.scrollHeight - el.scrollTop - el.clientHeight < 120;
  }

  async function send() {
    const text = draft.trim();
    if (!text || sending) return;
    const clientMessageId = crypto.randomUUID();
    const mine = pendingUserLine(clientMessageId, text);
    setDraft("");
    setSendError(null);
    setSending(true);
    setExtras((prev) => [...prev, mine]);
    pinnedToBottom.current = true;
    try {
      const result = await sendRoomMessage(token, text, clientMessageId);
      const fresh = result.messages
        .map((payload) => admitLive(seen.current, payload))
        .filter((line): line is ChatMessage => line !== null);
      setExtras((prev) => [
        ...prev.map((m) => (m.key === mine.key ? { ...m, pending: false } : m)),
        ...fresh,
      ]);
      onTurnComplete();
    } catch (e) {
      if (isAuthError(e)) {
        onAuthLost();
        return;
      }
      // give the words back: drop the optimistic line, restore the draft
      setExtras((prev) => prev.filter((m) => m.key !== mine.key));
      setDraft(text);
      setSendError(e instanceof Error ? e.message : "that didn’t go through");
    } finally {
      setSending(false);
    }
  }

  function onComposerKey(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  const byId = new Map(council.map((m) => [m.id, m]));
  const dateLine = room?.date
    ? new Date(`${room.date}T12:00:00`).toLocaleDateString(undefined, {
        weekday: "long",
        month: "long",
        day: "numeric",
      })
    : "";

  return (
    <div className="room">
      <header className="room-head">
        <div>
          <h2>today’s room</h2>
          <p className="room-date">{dateLine}</p>
        </div>
        <span
          className={`ws-dot ${STATUS_DOT[socketStatus]}`}
          title={`connection: ${socketStatus}`}
        />
      </header>

      <div className="room-log" ref={logRef} onScroll={onLogScroll}>
        {messages.length === 0 && !sending && (
          <p className="room-empty">
            it’s quiet in here. say hi — someone will hear you.
          </p>
        )}
        {messages.map((m, i) => {
          const prev = messages[i - 1];
          const grouped =
            prev &&
            prev.author === m.author &&
            prev.authorType === m.authorType &&
            !m.ephemeral &&
            !prev.ephemeral;
          if (m.authorType === "user") {
            return (
              <div
                key={m.key}
                className={`line mine${m.pending ? " pending" : ""}`}
              >
                <div className="bubble">{m.content}</div>
              </div>
            );
          }
          const member = byId.get(m.author);
          const hue = memberHue(m.author);
          return (
            <div
              key={m.key}
              className={`line theirs${m.ephemeral ? " ephemeral" : ""}`}
            >
              {!grouped && (
                <div className="line-head">
                  <span className="line-avatar" aria-hidden="true">
                    {member?.emoji ?? "✨"}
                  </span>
                  <span className="line-name" style={{ color: hue }}>
                    {member?.name ?? m.author}
                  </span>
                </div>
              )}
              <div className="bubble">{m.content}</div>
            </div>
          );
        })}
        {sending && (
          <p className="stirring">
            <span className="stir-dot" />
            <span className="stir-dot" />
            <span className="stir-dot" />
            the room is stirring
          </p>
        )}
      </div>

      {sendError && <p className="soft-error">{sendError}</p>}

      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault();
          send();
        }}
      >
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.currentTarget.value)}
          onKeyDown={onComposerKey}
          placeholder="say something…"
          rows={2}
          disabled={sending}
        />
        <button type="submit" disabled={sending || !draft.trim()}>
          send
        </button>
      </form>
    </div>
  );
}
