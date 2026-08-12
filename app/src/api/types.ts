// the /api/v1 contract, as the app consumes it. shapes mirror
// src/web/server.py handlers - if a field changes there, it changes here.

export interface CouncilMember {
  id: string;
  chair: boolean;
  emoji: string;
  lane: string;
  specialty: string;
  /** the name the user knows them by: persona override or authored id */
  name: string;
  /** override form if reshaped, else the authored species */
  species: string;
  status: "not_met" | "introducing" | "active" | "declined" | "disabled";
}

export interface RoomInfo {
  id: string;
  type: string;
  date: string | null;
  status: string;
}

export interface RoomCurrent {
  room: RoomInfo;
  chat_available: boolean;
}

/** a persisted room message (GET /rooms/current/messages) */
export interface HistoryRow {
  id: number;
  author: string;
  author_type: string;
  content: string;
  at: string | null;
  platform: string | null;
}

/** a delivered line (websocket payload / POST response message) */
export interface DeliveryPayload {
  type: string;
  id?: string;
  author: string;
  content: string;
  at?: string;
  ephemeral?: boolean;
}

export interface SendResult {
  ok: boolean;
  messages: DeliveryPayload[];
  replayed?: boolean;
}

export interface TaskRow {
  id: number;
  public_id: string;
  title: string;
  status: string;
  priority: string | null;
  scheduled: string | null;
  window: string | null;
  pom_estimate: number | null;
  plan_title: string | null;
  helper: string | null;
  description: string | null;
}

export interface TodayPayload {
  today: string;
  user: { name: string | null };
  pom_minutes: number;
  break_minutes: number;
  buckets: {
    overdue: TaskRow[];
    today: TaskRow[];
    in_progress: TaskRow[];
  };
  focus: {
    active_task_id: number | null;
    seconds: Record<string, number>;
  };
  server_time: string;
}
