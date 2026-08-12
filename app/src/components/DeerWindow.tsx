import { useCallback, useEffect, useRef, useState } from "react";
import { SERVER_URL } from "../api/client";
import {
  fetchSidecarState,
  handshake,
  SidecarSocket,
  startBlock,
  stopBlock,
  type SessionState,
} from "../api/sidecar";
import { storedToken } from "../lib/session";
import InlineContent from "./InlineContent";

const LINE_LINGER_MS = 12000;
const DEFAULT_MINUTES = 25;

function mmss(totalSeconds: number): string {
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

/** the deer's little window: presence without demand. a loaf, a clock that
 * fills up, and the occasional authored line in a bubble. */
export default function DeerWindow() {
  const [session, setSession] = useState<SessionState>({ active: false });
  const [line, setLine] = useState<string | null>(null);
  const [connected, setConnected] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const lineTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const showLine = useCallback((text: string) => {
    setLine(text);
    if (lineTimer.current !== null) clearTimeout(lineTimer.current);
    lineTimer.current = setTimeout(() => setLine(null), LINE_LINGER_MS);
  }, []);

  // hand the sidecar its credential once - the deer window shares the
  // shell's origin, so the stored device token is right here
  useEffect(() => {
    const token = storedToken();
    if (token) {
      handshake(token, SERVER_URL).catch(() => {
        // sidecar not up yet; the socket's reconnect will find it and the
        // next window open re-handshakes
      });
    }
    fetchSidecarState()
      .then((state) => {
        setSession(state.session);
        if (state.line) setLine(state.line);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    const socket = new SidecarSocket({
      onPush: (payload) => {
        if (payload.type === "state") setSession(payload.session);
        if (payload.type === "line") showLine(payload.text);
      },
      onStatus: setConnected,
    });
    socket.start();
    return () => socket.stop();
  }, [showLine]);

  // the countdown renders locally off ends_at - one second-hand, no polling
  useEffect(() => {
    if (!session.active) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [session.active]);

  async function onStart(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    try {
      const result = await startBlock(label.trim(), DEFAULT_MINUTES);
      setSession(result.session);
      showLine(result.line);
      setLabel("");
    } catch (err) {
      showLine(err instanceof Error ? err.message : "hm, that didn’t work");
    } finally {
      setBusy(false);
    }
  }

  async function onStop() {
    if (busy) return;
    setBusy(true);
    try {
      const result = await stopBlock();
      setSession(result.session);
      showLine(result.line);
    } catch (err) {
      showLine(err instanceof Error ? err.message : "hm, that didn’t work");
    } finally {
      setBusy(false);
    }
  }

  let remaining = 0;
  let filled = 0;
  if (session.active && session.ends_at && session.started_at) {
    const ends = Date.parse(session.ends_at);
    const started = Date.parse(session.started_at);
    remaining = Math.max(0, Math.round((ends - now) / 1000));
    filled = Math.min(1, Math.max(0, (now - started) / (ends - started)));
  }
  const perked = session.active && remaining <= 60;

  return (
    <div className="deer-window">
      <div className="deer-drag" data-tauri-drag-region="true">
        <span
          className={`deer-link-dot${connected ? " on" : ""}`}
          title={connected ? "the deer is home" : "looking for the sidecar…"}
        />
      </div>

      {line && (
        <div className="deer-bubble">
          <InlineContent content={line} />
        </div>
      )}

      <div
        className={`deer-self${session.active ? " watching" : " loafing"}${
          perked ? " perked" : ""
        }`}
        aria-hidden="true"
      >
        🦌
      </div>
      <p className="deer-caption">
        {session.active
          ? perked
            ? "ears perked — almost there"
            : "on watch beside you"
          : "loafing nearby"}
      </p>

      {session.active ? (
        <div className="deer-session">
          {session.label && <p className="deer-label">{session.label}</p>}
          <p className="deer-clock">{mmss(remaining)}</p>
          <div className="deer-fill">
            <div
              className="deer-fill-bar"
              style={{ width: `${filled * 100}%` }}
            />
          </div>
          <button className="deer-stop" onClick={onStop} disabled={busy}>
            stop early
          </button>
        </div>
      ) : (
        <form className="deer-start" onSubmit={onStart}>
          <input
            value={label}
            onChange={(e) => setLabel(e.currentTarget.value)}
            placeholder="what are we doing?"
            maxLength={200}
          />
          <button type="submit" disabled={busy}>
            one block · {DEFAULT_MINUTES} min
          </button>
        </form>
      )}
    </div>
  );
}
