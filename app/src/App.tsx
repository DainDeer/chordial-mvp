import { useCallback, useEffect, useState } from "react";
import LinkScreen from "./components/LinkScreen";
import PresenceRail from "./components/PresenceRail";
import Home from "./components/Home";
import Room, { type CycleRoomHandle } from "./components/Room";
import ArchiveRoom from "./components/ArchiveRoom";
import { fetchCouncil, isAuthError } from "./api/client";
import type { ArchivedRoom, CouncilMember } from "./api/types";
import { clearSession, storedToken, storeSession } from "./lib/session";

type View = "home" | "room" | "archive" | "cycle";

/** the shell: link screen until a device token exists, then the rail plus
 * whichever space you're in. losing auth anywhere (revoked device) drops
 * cleanly back to the front porch. */
export default function App() {
  const [token, setToken] = useState<string | null>(storedToken());
  const [view, setView] = useState<View>("home");
  const [council, setCouncil] = useState<CouncilMember[]>([]);
  const [archiveRoom, setArchiveRoom] = useState<ArchivedRoom | null>(null);
  const [cycleRoom, setCycleRoom] = useState<CycleRoomHandle | null>(null);

  const onAuthLost = useCallback(() => {
    clearSession();
    setToken(null);
    setCouncil([]);
    setView("home");
  }, []);

  const refreshCouncil = useCallback(() => {
    if (!token) return;
    fetchCouncil(token)
      .then((body) => setCouncil(body.council))
      .catch((e) => {
        if (isAuthError(e)) onAuthLost();
      });
  }, [token, onAuthLost]);

  useEffect(() => {
    refreshCouncil();
  }, [refreshCouncil]);

  if (!token) {
    return (
      <LinkScreen
        onLinked={(newToken, deviceId) => {
          storeSession(newToken, deviceId);
          setToken(newToken);
        }}
      />
    );
  }

  return (
    <div className="shell">
      <PresenceRail council={council} view={view} onNavigate={setView} />
      <main className="stage">
        {view === "home" && (
          <Home
            token={token}
            onEnterRoom={() => setView("room")}
            onEnterCycleRoom={(handle) => {
              setCycleRoom(handle);
              setView("cycle");
            }}
            onOpenArchive={(room) => {
              setArchiveRoom(room);
              setView("archive");
            }}
            onAuthLost={onAuthLost}
          />
        )}
        {view === "room" && (
          <Room
            token={token}
            council={council}
            onTurnComplete={refreshCouncil}
            onAuthLost={onAuthLost}
          />
        )}
        {view === "cycle" && cycleRoom && (
          <Room
            key={cycleRoom.id}
            token={token}
            council={council}
            cycleRoom={cycleRoom}
            onBack={() => setView("home")}
            onTurnComplete={refreshCouncil}
            onAuthLost={onAuthLost}
          />
        )}
        {view === "archive" && archiveRoom && (
          <ArchiveRoom
            token={token}
            council={council}
            room={archiveRoom}
            onEnterToday={() => setView("room")}
            onBack={() => setView("home")}
            onAuthLost={onAuthLost}
          />
        )}
      </main>
    </div>
  );
}
