import { useEffect, useState } from "react";
import { fetchToday, isAuthError } from "../api/client";
import type { TaskRow, TodayPayload } from "../api/types";

interface Props {
  token: string;
  onEnterRoom: () => void;
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

/** the landing: a greeting, the shape of today, and the door to the room. */
export default function Home({ token, onEnterRoom, onAuthLost }: Props) {
  const [today, setToday] = useState<TodayPayload | null>(null);
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

      <button className="enter-room" onClick={onEnterRoom}>
        step into today’s room →
      </button>
    </div>
  );
}
