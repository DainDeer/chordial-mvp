import { useCallback, useEffect, useState } from "react";
import LinkScreen from "./components/LinkScreen";
import PresenceRail from "./components/PresenceRail";
import Home from "./components/Home";
import Room from "./components/Room";
import { fetchCouncil, isAuthError } from "./api/client";
import type { CouncilMember } from "./api/types";
import { clearSession, storedToken, storeSession } from "./lib/session";

type View = "home" | "room";

/** the shell: link screen until a device token exists, then the rail plus
 * whichever space you're in. losing auth anywhere (revoked device) drops
 * cleanly back to the front porch. */
export default function App() {
  const [token, setToken] = useState<string | null>(storedToken());
  const [view, setView] = useState<View>("home");
  const [council, setCouncil] = useState<CouncilMember[]>([]);

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
        {view === "home" ? (
          <Home
            token={token}
            onEnterRoom={() => setView("room")}
            onAuthLost={onAuthLost}
          />
        ) : (
          <Room
            token={token}
            council={council}
            onTurnComplete={refreshCouncil}
            onAuthLost={onAuthLost}
          />
        )}
      </main>
    </div>
  );
}
