import { useEffect, useState } from "react";
import { fetchArchive, fetchToday, isAuthError } from "../api/client";
import type { ArchivedRoom, TaskRow, TodayPayload } from "../api/types";
import CyclePanel from "./CyclePanel";

interface Props {
  token: string;
  onEnterRoom: () => void;
  onOpenArchive: (room: ArchivedRoom) => void;
  onAuthLost: () => void;
}

function greeting(now: Date): string {
  const h = now.getHours();
  if (h < 5) return "up late";
  if (h < 12) return "good morning";
  if (h < 18) return "good afternoon";
  return "good evening";
}

function TaskList({ label, tasks }: { label: string; tasks: TaskRow[] }) {
  if (tasks.length === 0) return null;
  return (
    <section className="task-group">
      <h3>{label}</h3>
      <ul>
        {tasks.map((t) => (
          <li key={t.id} className="task-row">
            <span
              className={`task-dot ${t.priority ?? "none"}`}
              aria-hidden="true"
            />
            <span className="task-title">{t.title}</span>
            {t.plan_title && <span className="task-plan">{t.plan_title}</span>}
          </li>
        ))}
      </ul>
    </section>
  );
}

/** the landing: a greeting, the shape of today, the shared cycle, the door
 * to the room, and the journal of remembered days. */
export default function Home({
  token,
  onEnterRoom,
  onOpenArchive,
  onAuthLost,
}: Props) {
  const [today, setToday] = useState<TodayPayload | null>(null);
  const [pastDays, setPastDays] = useState<ArchivedRoom[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchToday(token)
      .then((payload) => {
        if (!cancelled) setToday(payload);
      })
      .catch((e) => {
        if (cancelled) return;
        if (isAuthError(e)) onAuthLost();
        else setError(e instanceof Error ? e.message : "couldn’t load today");
      });
    fetchArchive(token)
      .then((body) => {
        if (cancelled) return;
        // the journal shows what's settled: closed days only
        setPastDays(body.rooms.filter((r) => r.status === "closed"));
      })
      .catch((e) => {
        if (!cancelled && isAuthError(e)) onAuthLost();
        // a missing journal is not an error worth a banner
      });
    return () => {
      cancelled = true;
    };
  }, [token, onAuthLost]);

  const name = today?.user.name;
  const buckets = today?.buckets;
  const empty =
    buckets &&
    buckets.today.length === 0 &&
    buckets.overdue.length === 0 &&
    buckets.in_progress.length === 0;

  const dateLine = today
    ? new Date(`${today.today}T12:00:00`).toLocaleDateString(undefined, {
        weekday: "long",
        month: "long",
        day: "numeric",
      })
    : "";

  return (
    <div className="home">
      <header className="home-head">
        <h1>
          {greeting(new Date())}
          {name ? `, ${name}` : ""}
        </h1>
        <p className="home-date">{dateLine}</p>
      </header>

      {error && <p className="soft-error">{error}</p>}

      {buckets && (
        <div className="home-tasks">
          <TaskList label="in motion" tasks={buckets.in_progress} />
          <TaskList label="today" tasks={buckets.today} />
          <TaskList label="carried over" tasks={buckets.overdue} />
          {empty && (
            <p className="home-empty">
              nothing on the list — a quiet day is allowed.
            </p>
          )}
        </div>
      )}

      <CyclePanel
        token={token}
        pomMinutes={today?.pom_minutes ?? 25}
        onAuthLost={onAuthLost}
      />

      <button className="enter-room" onClick={onEnterRoom}>
        step into today’s room →
      </button>

      {pastDays.length > 0 && (
        <section className="past-days">
          <h3>remembered days</h3>
          <ul>
            {pastDays.map((r) => (
              <li key={r.room_uuid}>
                <button
                  className="past-day"
                  onClick={() => onOpenArchive(r)}
                >
                  <span className="past-day-date">
                    {r.date
                      ? new Date(`${r.date}T12:00:00`).toLocaleDateString(
                          undefined,
                          { weekday: "short", month: "short", day: "numeric" },
                        )
                      : "before the rooms"}
                  </span>
                  {r.summary && (
                    <span className="past-day-hint">
                      {r.summary.split("\n")[0]}
                    </span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
