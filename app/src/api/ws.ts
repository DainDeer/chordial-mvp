// the live channel: ONE websocket for the whole app (owned by the shell
// since 7a, so it lives whenever the window is open - proactive lines
// confirm even from the home screen, and presence heartbeats ride it),
// carrying every line the council delivers to this user (attribution rides
// in the payload - one socket, whole council). auth is first-message
// {token} because webviews can't set headers on websocket upgrades.

import { SERVER_URL } from "./client";
import type { DeliveryPayload } from "./types";

export type SocketStatus = "connecting" | "open" | "closed" | "revoked";

export interface RoomSocketHandlers {
  onPayload: (payload: DeliveryPayload) => void;
  onStatus: (status: SocketStatus) => void;
  /** fires on every re-open AFTER the first - the moment to refetch history,
   * since anything delivered while the socket was down never reached us */
  onReconnect: () => void;
}

const BACKOFF_START_MS = 1000;
const BACKOFF_MAX_MS = 15000;

export class RoomSocket {
  private token: string;
  private handlers: RoomSocketHandlers;
  private ws: WebSocket | null = null;
  private stopped = false;
  private everOpened = false;
  private backoffMs = BACKOFF_START_MS;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(token: string, handlers: RoomSocketHandlers) {
    this.token = token;
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
      this.ws.onclose = null; // a deliberate stop is not a reconnect case
      this.ws.close();
      this.ws = null;
    }
  }

  private connect(): void {
    if (this.stopped) return;
    this.handlers.onStatus("connecting");
    const url = SERVER_URL.replace(/^http/, "ws") + "/api/v1/ws";
    const ws = new WebSocket(url);
    this.ws = ws;

    ws.onopen = () => ws.send(JSON.stringify({ token: this.token }));

    ws.onmessage = (event) => {
      let payload: DeliveryPayload | { type: string };
      try {
        payload = JSON.parse(event.data);
      } catch {
        return;
      }
      if (payload.type === "hello") {
        this.backoffMs = BACKOFF_START_MS;
        this.handlers.onStatus("open");
        if (this.everOpened) this.handlers.onReconnect();
        this.everOpened = true;
        return;
      }
      if (payload.type === "message") {
        this.handlers.onPayload(payload as DeliveryPayload);
      }
    };

    ws.onclose = (event) => {
      this.ws = null;
      if (this.stopped) return;
      if (event.code === 4401) {
        // the device was revoked: reconnecting would loop forever
        this.handlers.onStatus("revoked");
        this.stopped = true;
        return;
      }
      this.handlers.onStatus("closed");
      this.reconnectTimer = setTimeout(() => this.connect(), this.backoffMs);
      this.backoffMs = Math.min(this.backoffMs * 2, BACKOFF_MAX_MS);
    };
  }

  /** fire-and-forget a client frame (the presence heartbeat). dropped
   * silently while the socket is down - presence going quiet IS the
   * away signal, so there is nothing to queue. */
  send(frame: object): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(frame));
    }
  }
}

/** fan-out for the shell-owned socket: views subscribe for the payloads
 * and reconnects they care about while mounted, and the shell keeps the
 * one socket alive across navigation. */
export class SocketBus {
  private payloadListeners = new Set<(payload: DeliveryPayload) => void>();
  private reconnectListeners = new Set<() => void>();

  onPayload(fn: (payload: DeliveryPayload) => void): () => void {
    this.payloadListeners.add(fn);
    return () => this.payloadListeners.delete(fn);
  }

  onReconnect(fn: () => void): () => void {
    this.reconnectListeners.add(fn);
    return () => this.reconnectListeners.delete(fn);
  }

  emitPayload(payload: DeliveryPayload): void {
    for (const fn of this.payloadListeners) fn(payload);
  }

  emitReconnect(): void {
    for (const fn of this.reconnectListeners) fn();
  }
}
