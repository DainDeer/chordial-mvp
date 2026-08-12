// the deer window's line to the sidecar: loopback http + a push websocket.
// no bearer auth here - the sidecar binds 127.0.0.1 and gates browsers by
// exact origin; the handshake below is how it RECEIVES the device
// credential (for its outbox sync up to the chordial server).

export const SIDECAR_URL: string =
  import.meta.env.VITE_CHORDIAL_SIDECAR ?? "http://localhost:8485";

export interface SessionState {
  active: boolean;
  label?: string | null;
  minutes?: number;
  started_at?: string;
  ends_at?: string;
  remaining_seconds?: number;
}

export interface SidecarState {
  session: SessionState;
  line: string | null;
  linked: boolean;
  sync_error: string | null;
}

export type SidecarPush =
  | { type: "state"; session: SessionState }
  | { type: "line"; moment: string; text: string };

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const resp = await fetch(`${SIDECAR_URL}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  const body = await resp.json().catch(() => null);
  if (!resp.ok) {
    const message =
      body && typeof body.error === "string"
        ? body.error
        : `sidecar request failed (${resp.status})`;
    throw new Error(message);
  }
  return body as T;
}

export const fetchSidecarState = () => request<SidecarState>("/v1/state");

export const handshake = (token: string, serverUrl: string) =>
  request<{ ok: boolean }>("/v1/handshake", {
    method: "POST",
    body: JSON.stringify({ token, server_url: serverUrl }),
  });

export const startBlock = (label: string, minutes: number) =>
  request<{ session: SessionState; line: string }>("/v1/session/start", {
    method: "POST",
    body: JSON.stringify({ label: label || null, minutes }),
  });

export const stopBlock = () =>
  request<{ session: SessionState; outcome: string; line: string }>(
    "/v1/session/stop",
    { method: "POST" },
  );

const BACKOFF_START_MS = 1000;
const BACKOFF_MAX_MS = 15000;

/** the push channel, with quiet reconnection - the deer window may outlive
 * many sidecar restarts and should just find her again. */
export class SidecarSocket {
  private handlers: {
    onPush: (payload: SidecarPush) => void;
    onStatus: (connected: boolean) => void;
  };
  private ws: WebSocket | null = null;
  private stopped = false;
  private backoffMs = BACKOFF_START_MS;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(handlers: {
    onPush: (payload: SidecarPush) => void;
    onStatus: (connected: boolean) => void;
  }) {
    this.handlers = handlers;
  }

  start(): void {
    this.stopped = false;
    this.connect();
  }

  stop(): void {
    this.stopped = true;
    if (this.reconnectTimer !== null) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    if (this.ws) {
      this.ws.onclose = null;
      this.ws.close();
      this.ws = null;
    }
  }

  private connect(): void {
    if (this.stopped) return;
    const ws = new WebSocket(SIDECAR_URL.replace(/^http/, "ws") + "/v1/ws");
    this.ws = ws;
    ws.onopen = () => {
      this.backoffMs = BACKOFF_START_MS;
      this.handlers.onStatus(true);
    };
    ws.onmessage = (event) => {
      try {
        this.handlers.onPush(JSON.parse(event.data));
      } catch {
        // not ours to worry about
      }
    };
    ws.onclose = () => {
      this.ws = null;
      if (this.stopped) return;
      this.handlers.onStatus(false);
      this.reconnectTimer = setTimeout(() => this.connect(), this.backoffMs);
      this.backoffMs = Math.min(this.backoffMs * 2, BACKOFF_MAX_MS);
    };
  }
}
