import type { CouncilMember } from "../api/types";
import { memberHue } from "../lib/council";

interface Props {
  council: CouncilMember[];
  view: "home" | "room";
  onNavigate: (view: "home" | "room") => void;
}

/** the left rail: where you are, and who lives here. unmet members render
 * dimmed - residents you haven't been introduced to yet, not absences. */
export default function PresenceRail({ council, view, onNavigate }: Props) {
  const visible = council.filter((m) => m.status !== "declined");

  return (
    <nav className="rail">
      <button
        className="rail-mark"
        onClick={() => onNavigate("home")}
        title="home"
      >
        <span aria-hidden="true">🦌</span> chordial
      </button>

      <div className="rail-nav">
        <button
          className={view === "home" ? "rail-link active" : "rail-link"}
          onClick={() => onNavigate("home")}
        >
          home
        </button>
        <button
          className={view === "room" ? "rail-link active" : "rail-link"}
          onClick={() => onNavigate("room")}
        >
          today’s room
        </button>
      </div>

      <div className="rail-council">
        <span className="rail-label">the council</span>
        <ul>
          {visible.map((m) => {
            const met = m.status === "active" || m.status === "introducing";
            return (
              <li
                key={m.id}
                className={met ? "member" : "member unmet"}
                title={met ? m.specialty : "hasn’t dropped by yet"}
              >
                <span
                  className="member-avatar"
                  style={{ borderColor: met ? memberHue(m.id) : "transparent" }}
                  aria-hidden="true"
                >
                  {m.emoji}
                </span>
                <span className="member-text">
                  <span className="member-name">{m.name}</span>
                  <span className="member-lane">
                    {met ? m.lane : "not yet met"}
                  </span>
                </span>
              </li>
            );
          })}
        </ul>
      </div>
    </nav>
  );
}
