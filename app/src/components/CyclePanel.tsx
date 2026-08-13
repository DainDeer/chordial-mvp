import { useCallback, useEffect, useState } from "react";
import { fetchCycle, isAuthError } from "../api/client";
import { startFocus } from "../api/sidecar";
import type { CommitmentRow, CyclePayload } from "../api/types";
import {
  capacityFill,
  capacityLine,
  commitmentFill,
  visibleCommitments,
} from "../lib/cycle";

interface Props {
  token: string;
  pomMinutes: number;
  onAuthLost: () => void;
}

/** the shared cycle state on the home screen: theme, capacity, every
 * commitment with its real progress (banked minutes flow in from the deer
 * through the sync contract), and the one-tap door - a next action starts
 * a block on the deer's clock. */
export default function CyclePanel({ token, pomMinutes, onAuthLost }: Props) {
  const [view, setView] = useState<CyclePayload | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [startingId, setStartingId] = useState<number | null>(null);

  const refresh = useCallback(() => {
    fetchCycle(token)
      .then(setView)
      .catch((e) => {
        if (isAuthError(e)) onAuthLost();
        // otherwise stay quietly absent - the panel is a window, not a wall
      });
  }, [token, onAuthLost]);

  useEffect(() => {
    refresh();
    // the projection moves when blocks land; a slow poll keeps the panel
    // honest while the app sits open (the deer syncs every ~15s)
    const timer = setInterval(refresh, 20_000);
    return () => clearInterval(timer);
  }, [refresh]);

  async function startBlock(c: CommitmentRow) {
    setNote(null);
    setStartingId(c.id);
    try {
      await startFocus(
        c.task_id,
        c.next_action ?? c.task_title ?? c.title,
        pomMinutes,
      );
      setNote("clock started — the deer has it 🦌");
    } catch {
      setNote("the deer isn’t home — is the sidecar running?");
    } finally {
      setStartingId(null);
      window.setTimeout(() => setNote(null), 5000);
    }
  }

  if (!view || !view.cycle) return null;
  const { cycle, totals } = view;
  const commitments = visibleCommitments(view.commitments ?? []);
  const bar = totals ? capacityFill(totals) : null;

  return (
    <section className="cycle-panel">
      <header className="cycle-head">
        <div>
          <h3>the cycle</h3>
          {cycle.theme && <p className="cycle-theme">{cycle.theme}</p>}
        </div>
        {view.frozen && (
          <span className="cycle-frozen" title="the baseline is frozen">
            baseline set
          </span>
        )}
      </header>

      {totals && bar && (
        <div className="cycle-capacity">
          <div className="cycle-bar" aria-hidden="true">
            <div
              className="cycle-bar-planned"
              style={{ width: `${bar.planned * 100}%` }}
            />
            <div
              className="cycle-bar-done"
              style={{ width: `${bar.done * 100}%` }}
            />
          </div>
          <p className="cycle-capacity-line">{capacityLine(totals)}</p>
        </div>
      )}

      <ul className="cycle-commitments">
        {commitments.map((c) => {
          const done = c.status === "completed";
          return (
            <li key={c.uuid} className={`commitment${done ? " done" : ""}`}>
              <div className="commitment-row">
                <span
                  className={`task-dot ${c.priority ?? "none"}`}
                  aria-hidden="true"
                />
                <span className="commitment-title">
                  {done ? "✓ " : ""}
                  {c.title}
                </span>
                <span className="commitment-blocks">
                  {c.blocks_done}
                  {c.blocks_planned != null && ` / ${c.blocks_planned}`}
                </span>
              </div>
              <div className="commitment-fill" aria-hidden="true">
                <div
                  className="commitment-fill-bar"
                  style={{ width: `${commitmentFill(c) * 100}%` }}
                />
              </div>
              {!done && c.next_action && (
                <div className="next-action">
                  <span className="next-action-label">next</span>
                  <span className="next-action-text">{c.next_action}</span>
                  <button
                    className="next-action-start"
                    onClick={() => startBlock(c)}
                    disabled={startingId !== null}
                  >
                    {startingId === c.id ? "starting…" : "start a block ▸"}
                  </button>
                </div>
              )}
            </li>
          );
        })}
      </ul>

      {note && <p className="cycle-note">{note}</p>}
    </section>
  );
}
