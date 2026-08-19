"""the app interface's presence trichotomy (ROOMS_DESIGN section 9, 7a) and
the mirror-aware payload shape.

presence answers 'deer bubble or tether?' for the pulse: absent = no
connected surface; idle = connected but no surface demonstrably attended;
active = at least one surface with a fresh non-idle heartbeat - or a user
whose surfaces have never reported at all (fail-open for pre-7a clients).
reports are PER CONNECTION and aggregated any-active-wins (sol's 7a P2: an
idle laptop's heartbeat must never overwrite an active desktop's). no
database, no sockets: the registry is in-memory arithmetic over monotonic
time.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Config  # noqa: E402
from src.providers.platforms.app import AppInterface, _PRESENCE_STALE_SECONDS  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def test_absent_without_any_connected_surface():
    app = AppInterface()
    assert app.presence_state("u1") == "absent"

    # a heartbeat alone never makes someone present - the surface must be
    # connected for a delivery to land
    app.note_presence("u1", 0)
    assert app.presence_state("u1") == "absent"


def test_connected_and_fresh_heartbeat_is_active():
    app = AppInterface()
    app.subscribe("u1")
    app.note_presence("u1", 12.5)
    assert app.presence_state("u1") == "active"


def test_connected_without_any_report_fails_open_to_active():
    """a pre-7a client never heartbeats; its connected surface keeps its
    lines rather than silently rerouting them to the phone."""
    app = AppInterface()
    app.subscribe("u1")
    assert app.presence_state("u1") == "active"


def test_idle_past_the_threshold_is_idle():
    app = AppInterface()
    app.subscribe("u1")
    app.note_presence("u1", Config.PRESENCE_IDLE_SECONDS)
    assert app.presence_state("u1") == "idle"

    # walking back to the desk flips it straight back
    app.note_presence("u1", 3)
    assert app.presence_state("u1") == "active"


def test_unknown_idle_counts_as_active():
    """no sidecar (dev rigs): the app reports openness alone, and openness
    is the best signal available."""
    app = AppInterface()
    app.subscribe("u1")
    app.note_presence("u1", None)
    assert app.presence_state("u1") == "active"
    app.note_presence("u1", "not-a-number")
    assert app.presence_state("u1") == "active"


def test_stale_heartbeats_on_a_live_socket_read_as_idle(monkeypatch):
    """the reporter died but the socket lives: an unreporting machine can't
    prove anyone is at it - away is the honest read."""
    import src.providers.platforms.app as app_mod

    clock = {"t": 1000.0}
    monkeypatch.setattr(app_mod.time, "monotonic", lambda: clock["t"])

    app = AppInterface()
    app.subscribe("u1")
    app.note_presence("u1", 0)
    assert app.presence_state("u1") == "active"

    clock["t"] += _PRESENCE_STALE_SECONDS + 1
    assert app.presence_state("u1") == "idle"


def test_disconnect_prunes_presence_and_reads_absent():
    app = AppInterface()
    queue = app.subscribe("u1")
    app.note_presence("u1", 0, connection=queue)
    app.unsubscribe("u1", queue)

    assert app.presence_state("u1") == "absent"
    assert app._presence == {}  # the registry doesn't grow forever


# --- multiple devices (sol's 7a P2): aggregate, never last-writer-wins --------


def test_an_idle_laptop_never_overwrites_an_active_desktop():
    app = AppInterface()
    desktop = app.subscribe("u1")
    laptop = app.subscribe("u1")

    # the idle laptop reports AFTER the active desktop - under
    # last-writer-wins this read idle and misrouted to the phone
    app.note_presence("u1", 5, connection=desktop)
    app.note_presence("u1", 9000, connection=laptop)
    assert app.presence_state("u1") == "active"

    # order-independent: the reverse interleaving reads the same
    app.note_presence("u1", 9000, connection=laptop)
    app.note_presence("u1", 5, connection=desktop)
    assert app.presence_state("u1") == "active"


def test_the_active_surface_leaving_takes_its_evidence_along():
    app = AppInterface()
    desktop = app.subscribe("u1")
    laptop = app.subscribe("u1")
    app.note_presence("u1", 5, connection=desktop)
    app.note_presence("u1", 9000, connection=laptop)

    app.unsubscribe("u1", desktop)
    assert app.presence_state("u1") == "idle"   # only the idle laptop remains
    app.unsubscribe("u1", laptop)
    assert app.presence_state("u1") == "absent"


def test_a_transient_reportless_queue_never_flips_idle_to_active():
    """an in-flight POST subscribes a queue with no heartbeat; while real
    reporters exist, that silent surface is not evidence of anyone."""
    app = AppInterface()
    laptop = app.subscribe("u1")
    app.note_presence("u1", 9000, connection=laptop)
    assert app.presence_state("u1") == "idle"

    post_queue = app.subscribe("u1")             # a send in flight
    assert app.presence_state("u1") == "idle"    # still nobody at a screen
    app.unsubscribe("u1", post_queue)
    assert app.presence_state("u1") == "idle"


def test_mirror_extras_ride_the_payload():
    """the tether mirror's payload carries author_type + platform; ordinary
    deliveries stay byte-for-byte pre-7a (neither key present)."""
    app = AppInterface()
    queue = app.subscribe("u1")

    assert run(app.send_message("u1", "from the phone", speaker="user",
                                author_type="user",
                                source_platform="telegram")) is True
    mirrored = queue.get_nowait()
    assert mirrored["author"] == "user"
    assert mirrored["author_type"] == "user"
    assert mirrored["platform"] == "telegram"

    assert run(app.send_message("u1", "an ordinary line",
                                speaker="vel", stream_id="room-1")) is True
    plain = queue.get_nowait()
    assert "author_type" not in plain
    assert "platform" not in plain
    assert plain["room"] == "room-1"
