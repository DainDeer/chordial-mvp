// the http side of the /api/v1 contract. every call carries the device
// bearer token; a 401 means the device was revoked (or the token is stale),
// which the app treats as "return to the link screen".

import type {
  CouncilMember,
  RoomCurrent,
  HistoryRow,
  SendResult,
  TaskRow,
  TodayPayload,
} from "./types";

// dev-mode: the python server runs beside `tauri dev` on its usual port.
// override with VITE_CHORDIAL_SERVER when the server lives elsewhere.
export const SERVER_URL: string =
  import.meta.env.VITE_CHORDIAL_SERVER ?? "http://localhost:8484";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/** the device was revoked or the token is stale - time to re-link */
export const isAuthError = (e: unknown) =>
  e instanceof ApiError && e.status === 401;

async function request<T>(
  path: string,
  init: RequestInit & { token?: string } = {},
): Promise<T> {
  const { token, ...rest } = init;
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(rest.headers as Record<string, string> | undefined),
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const resp = await fetch(`${SERVER_URL}${path}`, { ...rest, headers });
  const body = await resp.json().catch(() => null);
  if (!resp.ok) {
    const message =
      body && typeof body.error === "string"
        ? body.error
        : `request failed (${resp.status})`;
    throw new ApiError(resp.status, message);
  }
  return body as T;
}

export function linkDevice(
  code: string,
  name: string,
): Promise<{ device_id: string; token: string }> {
  return request("/api/v1/devices/link", {
    method: "POST",
    body: JSON.stringify({ code: code.trim(), name }),
  });
}

export function fetchCouncil(token: string): Promise<{ council: CouncilMember[] }> {
  return request("/api/v1/council", { token });
}

export function fetchRoomCurrent(token: string): Promise<RoomCurrent> {
  return request("/api/v1/rooms/current", { token });
}

export function fetchRoomMessages(
  token: string,
  limit = 100,
): Promise<{ messages: HistoryRow[] }> {
  return request(`/api/v1/rooms/current/messages?limit=${limit}`, { token });
}

export function fetchToday(token: string): Promise<TodayPayload> {
  return request("/api/v1/today", { token });
}

/** quick-add from the deer window: title only, lands scheduled-today */
export function createTask(
  token: string,
  title: string,
): Promise<{ ok: boolean; task: TaskRow }> {
  return request("/api/v1/tasks", {
    method: "POST",
    token,
    body: JSON.stringify({ title }),
  });
}

export function setTaskStatus(
  token: string,
  taskId: number,
  status: string,
): Promise<{ ok: boolean; task: TaskRow }> {
  return request(`/api/v1/tasks/${taskId}/status`, {
    method: "POST",
    token,
    body: JSON.stringify({ status }),
  });
}

// a turn can be slow (the model is thinking) and a duplicate POST for the
// same client_message_id while the first is in flight gets a 409 - wait and
// re-ask; the receipt layer replays the finished answer without a second turn.
const IN_FLIGHT_RETRY_MS = 2500;
const IN_FLIGHT_RETRIES = 8;

export async function sendRoomMessage(
  token: string,
  text: string,
  clientMessageId: string,
): Promise<SendResult> {
  for (let attempt = 0; ; attempt++) {
    try {
      return await request<SendResult>("/api/v1/rooms/current/messages", {
        method: "POST",
        token,
        body: JSON.stringify({
          text,
          client_message_id: clientMessageId,
        }),
      });
    } catch (e) {
      if (
        e instanceof ApiError &&
        e.status === 409 &&
        attempt < IN_FLIGHT_RETRIES
      ) {
        await new Promise((r) => setTimeout(r, IN_FLIGHT_RETRY_MS));
        continue;
      }
      throw e;
    }
  }
}
