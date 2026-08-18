import { useCallback, useEffect, useRef, useState } from "react";
import LinkScreen from "./components/LinkScreen";
import PresenceRail from "./components/PresenceRail";
import Home from "./components/Home";
import Room, { type CycleRoomHandle } from "./components/Room";
import ArchiveRoom from "./components/ArchiveRoom";
import { fetchCouncil, isAuthError } from "./api/client";
import { fetchSidecarState } from "./api/sidecar";
import { RoomSocket, SocketBus, type SocketStatus } from "./api/ws";
import type { ArchivedRoom, CouncilMember } from "./api/types";
import { clearSession, storedToken, storeSession } from "./lib/session";

type View = "home" | "room" | "archive" | "cycle";

// heartbeat every 30s; the server counts ~3 missed beats as away
const PRESENCE_BEAT_MS = 30_000;

/** the shell: link screen until a device token exists, then the rail plus
 * whichever space you're in. losing auth anywhere (revoked device) drops
 * cleanly back to the front porch.
 *
 * the shell owns the ONE server socket (7a): it stays up across navigation
 * so proactive lines land (and confirm) even from the home screen, presence
 * heartbeats ride it, and the room views subscribe through a bus while
 * mounted. lines that arrive while nobody is watching the room become the
 * door's unread nudge. */
export default function App() {
  const [token, setToken] = useState<string | null>(storedToken());
  const [view, setView] = useState<View>("home");
  const [council, setCouncil] = useState<CouncilMember[]>([]);
  const [archiveRoom, setArchiveRoom] = useState<ArchivedRoom | null>(null);
  const [cycleRoom, setCycleRoom] = useState<CycleRoomHandle | null>(null);
  const [socketStatus, setSocketStatus] = useState<SocketStatus>("connecting");
  const [unread, setUnread] = useState(0);

  const busRef = useRef<SocketBus | null>(null);
  if (!busRef.current) busRef.current = new SocketBus();
  const bus = busRef.current;

  // the socket callback checks the CURRENT view without resubscribing on
  // every navigation
  const viewRef = useRef(view);
  viewRef.current = view;

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

  useEffect(() => {
    if (!token) return;
    const socket = new RoomSocket(token, {
      onPayload: (payload) => {
        bus.emitPayload(payload);
        // a council line landing while nobody is in a room view becomes
        // the door's nudge. the person's own mirrored phone lines don't
        // count - the reply that follows them will.
        const watching =
          viewRef.current === "room" || viewRef.current === "cycle";
        if (!watching && payload.author_type !== "user") {
          setUnread((n) => n + 1);
        }
      },
      onStatus: (status) => {
        setSocketStatus(status);
        if (status === "revoked") onAuthLost();
      },
      onReconnect: () => bus.emitReconnect(),
    });
    socket.start();

    // the presence heartbeat (7a): the sidecar's OS idle clock, reported
    // over the socket so the server can route proactive words to the desk
    // or the phone. no sidecar (or a stale collector) reports openness
    // alone - the server treats that as active, the best signal available.
    let cancelled = false;
    const beat = async () => {
      let idle: number | undefined;
      try {
        const state = await fetchSidecarState();
        if (state.activity?.fresh) idle = state.activity.idle_seconds;
      } catch {
        idle = undefined;
      }
      if (!cancelled) {
        socket.send({ type: "presence", idle_seconds: idle });
      }
    };
    void beat();
    const timer = setInterval(() => void beat(), PRESENCE_BEAT_MS);

    return () => {
      cancelled = true;
      clearInterval(timer);
      socket.stop();
    };
  }, [token, bus, onAuthLost]);

  // stepping into a room clears the door's nudge
  useEffect(() => {
    if (view === "room" || view === "cycle") setUnread(0);
  }, [view]);

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
            unread={unread}
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
            bus={bus}
            socketStatus={socketStatus}
            onTurnComplete={refreshCouncil}
            onAuthLost={onAuthLost}
          />
        )}
        {view === "cycle" && cycleRoom && (
          <Room
            key={cycleRoom.id}
            token={token}
            council={council}
            bus={bus}
            socketStatus={socketStatus}
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
