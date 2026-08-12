import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  createTask,
  fetchToday,
  SERVER_URL,
  setTaskStatus,
} from "../api/client";
import {
  fetchSidecarState,
  finishFocus,
  handshake,
  pauseFocus,
  SidecarSocket,
  startFocus,
  type ActivityFlags,
  type FocusState,
} from "../api/sidecar";
import type { DoneTaskRow, TaskRow, TodayPayload } from "../api/types";
import {
  addPendingDone,
  listPendingDone,
  removePendingDone,
} from "../lib/pendingDone";
import { storedToken, TOKEN_STORAGE_KEY } from "../lib/session";
import Confetti from "./Confetti";
import InlineContent from "./InlineContent";

const LINE_LINGER_MS = 12000;
const TOKEN_POLL_MS = 2500;
const PENDING_RETRY_MS = 10000;

function mmss(totalSeconds: number): string {
  const m = Math.floor(totalSeconds / 60);
  const s = Math.max(0, Math.round(totalSeconds % 60));
  return `${m}:${String(s).padStart(2, "0")}`;
}

/** the deer's window: the day's tasks with their banked-time bars, one
 * running clock, and a small creature keeping you company. tasks are
 * canonical on the server; the clock and the banked minutes live in the
 * sidecar; this window is where they meet. */
export default function DeerWindow() {
  // reactive, not read-once: the person links in the MAIN window while
  // this one is already open. the cross-window storage event is the fast
  // path; a slow poll is the belt (webview storage-event delivery varies)
  const [token, setToken] = useState<string | null>(storedToken);
  const [focus, setFocus] = useState<FocusState>({
    running: false,
    banked: {},
  });
  const [pendingDone, setPendingDone] = useState<number[]>(() =>
    listPendingDone(window.localStorage),
  );
  const [activity, setActivity] = useState<ActivityFlags | null>(null);
  const [today, setToday] = useState<TodayPayload | null>(null);
  const [line, setLine] = useState<string | null>(null);
  const [connected, setConnected] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const [newTitle, setNewTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirmingFinish, setConfirmingFinish] = useState(false);
  const [celebrating, setCelebrating] = useState(false);
  const lineTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const showLine = useCallback((text: string) => {
    setLine(text);
    if (lineTimer.current !== null) clearTimeout(lineTimer.current);
    lineTimer.current = setTimeout(() => setLine(null), LINE_LINGER_MS);
  }, []);

  const refreshToday = useCallback(() => {
    if (!token) return;
    fetchToday(token)
      .then(setToday)
      .catch(() => {});
  }, [token]);

  // notice link/relink/revoke from the main window: storage event + poll
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === TOKEN_STORAGE_KEY || e.key === null) {
        setToken(storedToken());
      }
    };
    window.addEventListener("storage", onStorage);
    const poll = setInterval(() => {
      setToken((current) => {
        const fresh = storedToken();
        return fresh === current ? current : fresh;
      });
    }, TOKEN_POLL_MS);
    return () => {
      window.removeEventListener("storage", onStorage);
      clearInterval(poll);
    };
  }, []);

  // hand the sidecar its credential whenever we have one AND can reach it -
  // covers first link, relink, and a sidecar that started after this window
  useEffect(() => {
    if (token && connected) {
      handshake(token, SERVER_URL).catch(() => {});
    }
  }, [token, connected]);

  useEffect(() => {
    fetchSidecarState()
      .then((state) => {
        setFocus(state.focus);
        if (state.activity) setActivity(state.activity);
        if (state.line) setLine(state.line);
      })
      .catch(() => {});
    refreshToday();
  }, [token, connected, refreshToday]);

  useEffect(() => {
    const socket = new SidecarSocket({
      onPush: (payload) => {
        if (payload.type === "state") {
          setFocus(payload.focus);
          if (payload.activity) setActivity(payload.activity);
        }
        if (payload.type === "line") showLine(payload.text);
      },
      onStatus: setConnected,
    });
    socket.start();
    return () => socket.stop();
  }, [showLine]);

  // the offline-finish ledger: retry canonical done-mutations until the
  // server confirms them; the rows render as "syncing" in the meantime
  useEffect(() => {
    if (!token || pendingDone.length === 0) return;
    let cancelled = false;

    const flush = async () => {
      let changed = false;
      for (const taskId of listPendingDone(window.localStorage)) {
        try {
          await setTaskStatus(token, taskId, "done");
          removePendingDone(window.localStorage, taskId);
          changed = true;
        } catch (err) {
          // a 404 means the task is gone - nothing left to complete
          if (err instanceof ApiError && err.status === 404) {
            removePendingDone(window.localStorage, taskId);
            changed = true;
          }
        }
      }
      if (cancelled) return;
      setPendingDone(listPendingDone(window.localStorage));
      if (changed) refreshToday();
    };

    flush();
    const timer = setInterval(flush, PENDING_RETRY_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [token, pendingDone.length, refreshToday]);

  // one local second-hand while a clock runs
  useEffect(() => {
    if (!focus.running) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [focus.running]);

  const pomMinutes = today?.pom_minutes ?? 25;
  const runSeconds =
    focus.running && focus.started_at
      ? Math.max(0, (now - Date.parse(focus.started_at)) / 1000)
      : 0;
  const targetSeconds = (focus.target_minutes ?? pomMinutes) * 60;
  const overtime = focus.running && runSeconds >= targetSeconds;

  const openTasks: TaskRow[] = today
    ? [
        ...today.buckets.in_progress,
        ...today.buckets.today,
        ...today.buckets.overdue,
      ]
    : [];
  const doneTasks: DoneTaskRow[] = today?.buckets.done ?? [];

  /** today's minutes on a task, live: banked runs + the running clock */
  function taskSeconds(taskId: number): number {
    const banked = focus.banked[String(taskId)] ?? 0;
    return focus.running && focus.task_id === taskId
      ? banked + runSeconds
      : banked;
  }

  function taskFill(task: TaskRow | DoneTaskRow): number {
    const target = Math.max(task.pom_estimate ?? 1, 0.5) * pomMinutes * 60;
    return Math.min(1, taskSeconds(task.id) / target);
  }

  async function onPickTask(task: TaskRow) {
    if (busy || (focus.running && focus.task_id === task.id)) return;
    setBusy(true);
    setConfirmingFinish(false);
    try {
      const result = await startFocus(task.id, task.title, pomMinutes);
      setFocus(result.focus);
      showLine(result.line);
    } catch (err) {
      showLine(err instanceof Error ? err.message : "hm, that didn’t work");
    } finally {
      setBusy(false);
    }
  }

  async function onPause() {
    if (busy) return;
    setBusy(true);
    setConfirmingFinish(false);
    try {
      const result = await pauseFocus();
      setFocus(result.focus);
      showLine(result.line);
    } catch (err) {
      showLine(err instanceof Error ? err.message : "hm, that didn’t work");
    } finally {
      setBusy(false);
    }
  }

  async function onFinish() {
    if (busy) return;
    // landing before the block is up deserves one gentle "you sure?"
    if (runSeconds < targetSeconds && !confirmingFinish) {
      setConfirmingFinish(true);
      return;
    }
    setBusy(true);
    setConfirmingFinish(false);
    const taskId = focus.task_id;
    try {
      const result = await finishFocus();
      setFocus(result.focus);
      showLine(result.line);
      setCelebrating(true);
      if (token && typeof taskId === "number") {
        try {
          await setTaskStatus(token, taskId, "done");
        } catch {
          // offline / server down: the finish is real, the mutation is
          // owed - persist it and retry until the server confirms
          setPendingDone(addPendingDone(window.localStorage, taskId));
        }
      }
      refreshToday();
    } catch (err) {
      showLine(err instanceof Error ? err.message : "hm, that didn’t work");
    } finally {
      setBusy(false);
    }
  }

  async function onAddTask(e: React.FormEvent) {
    // adding never touches the running clock - jot it, keep working,
    // switch when YOU choose
    e.preventDefault();
    const title = newTitle.trim();
    if (!title || !token || busy) return;
    setBusy(true);
    try {
      await createTask(token, title);
      setNewTitle("");
      refreshToday();
    } catch (err) {
      showLine(err instanceof Error ? err.message : "couldn’t add that");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="deer-window">
      {celebrating && <Confetti onDone={() => setCelebrating(false)} />}

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
        className={`deer-self${focus.running ? " watching" : " loafing"}${
          overtime ? " perked" : ""
        }${activity?.blocked ? " hushed" : ""}`}
        aria-hidden="true"
      >
        🦌
      </div>
      <p className="deer-caption">
        {activity?.blocked
          ? "hushed — meeting nearby"
          : activity?.drifting
            ? "*ears soft* you wandered — that’s allowed"
            : focus.running
              ? overtime
                ? "still here — every extra minute counts"
                : "on watch beside you"
              : "loafing nearby"}
      </p>

      {focus.running && (
        <div className="deer-session">
          {focus.label && <p className="deer-label">{focus.label}</p>}
          <p className={`deer-clock${overtime ? " over" : ""}`}>
            {mmss(runSeconds)}
            {overtime && (
              <span className="deer-extra">
                +{mmss(runSeconds - targetSeconds)} extra
              </span>
            )}
          </p>
          <div className="deer-fill">
            <div
              className="deer-fill-bar"
              style={{
                width: `${Math.min(1, runSeconds / targetSeconds) * 100}%`,
              }}
            />
          </div>
          <div className="deer-controls">
            <button className="deer-pause" onClick={onPause} disabled={busy}>
              pause
            </button>
            {confirmingFinish ? (
              <>
                <button
                  className="deer-finish confirm"
                  onClick={onFinish}
                  disabled={busy}
                >
                  finish early?
                </button>
                <button
                  className="deer-cancel"
                  onClick={() => setConfirmingFinish(false)}
                >
                  keep going
                </button>
              </>
            ) : (
              <button
                className="deer-finish"
                onClick={onFinish}
                disabled={busy}
              >
                finished ✓
              </button>
            )}
          </div>
        </div>
      )}

      {token ? (
        <div className="deer-tasks">
          <ul>
            {openTasks.map((task) => {
              const active = focus.running && focus.task_id === task.id;
              if (pendingDone.includes(task.id)) {
                // finished here, not yet confirmed by the server: visibly
                // done (never open-and-clickable), honestly still syncing
                return (
                  <li key={task.id}>
                    <div className="deer-task done syncing">
                      <span className="deer-task-check" aria-hidden="true">
                        ✓
                      </span>
                      <span className="deer-task-title">{task.title}</span>
                      <span className="deer-task-note">syncing…</span>
                    </div>
                  </li>
                );
              }
              return (
                <li key={task.id}>
                  <button
                    className={`deer-task${active ? " active" : ""}`}
                    onClick={() => onPickTask(task)}
                    disabled={busy || active}
                    title={active ? "this clock is running" : "start / switch"}
                  >
                    <span className="deer-task-title">{task.title}</span>
                    <span className="deer-task-bar">
                      <span
                        className="deer-task-fill"
                        style={{ width: `${taskFill(task) * 100}%` }}
                      />
                    </span>
                  </button>
                </li>
              );
            })}
            {doneTasks.map((task) => (
              <li key={`done-${task.id}`}>
                <div className="deer-task done">
                  <span className="deer-task-check" aria-hidden="true">
                    ✓
                  </span>
                  <span className="deer-task-title">{task.title}</span>
                  <span className="deer-task-bar">
                    <span
                      className="deer-task-fill done"
                      style={{ width: "100%" }}
                    />
                  </span>
                </div>
              </li>
            ))}
            {openTasks.length === 0 && doneTasks.length === 0 && (
              <li className="deer-empty">
                nothing on the list yet — jot one below.
              </li>
            )}
          </ul>
          <form className="deer-add" onSubmit={onAddTask}>
            <input
              value={newTitle}
              onChange={(e) => setNewTitle(e.currentTarget.value)}
              placeholder="add a task…"
              maxLength={300}
            />
            <button type="submit" disabled={busy || !newTitle.trim()}>
              +
            </button>
          </form>
        </div>
      ) : (
        <p className="deer-unlinked">
          link chordial in the main window first — then i can see your day.
        </p>
      )}
    </div>
  );
}
