"""onboarding now establishes the user's timezone (name -> timezone -> memory),
so check-ins land at the right local time instead of assuming UTC.

the timezone step is db-free (only touches user prefs), so a fake user_manager
keeps this fast and isolated.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.onboarding_service import OnboardingService  # noqa: E402
from src.utils.timezone_utils import resolve_timezone  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# --- resolve_timezone: freeform answer -> IANA name ------------------------

class TestResolveTimezone:
    def test_exact_iana_name_any_case(self):
        assert resolve_timezone("US/Pacific") == "US/Pacific"
        assert resolve_timezone("america/new_york") == "America/New_York"
        assert resolve_timezone("Europe/London") == "Europe/London"

    def test_abbreviations_map_dst_aware(self):
        # pytz has bare "EST"/"MST" (fixed offset) - we want the DST-aware region
        assert resolve_timezone("PST") == "US/Pacific"
        assert resolve_timezone("est") == "US/Eastern"
        assert resolve_timezone("mst") == "US/Mountain"

    def test_cities_and_regions(self):
        assert resolve_timezone("california") == "US/Pacific"
        assert resolve_timezone("New York") == "US/Eastern"
        assert resolve_timezone("london") == "Europe/London"
        assert resolve_timezone("tokyo") == "Asia/Tokyo"

    def test_full_sentences(self):
        assert resolve_timezone("i'm in california") == "US/Pacific"
        assert resolve_timezone("pacific time please") == "US/Pacific"
        assert resolve_timezone("i live near new york") == "US/Eastern"

    def test_unresolvable_returns_none(self):
        assert resolve_timezone("") is None
        assert resolve_timezone("   ") is None
        assert resolve_timezone("somewhere over the rainbow") is None

    def test_short_key_does_not_match_inside_a_word(self):
        # "est" must match as a whole word, not inside "interested"/"forest"
        assert resolve_timezone("i'm interested in the forest") is None


class FakeUserManager:
    def __init__(self):
        self.prefs = {}

    async def update_user_preferences(self, user_uuid, prefs):
        self.prefs.setdefault(user_uuid, {}).update(prefs)

    async def get_or_create_user(self, platform, platform_user_id, *a, **k):
        return "u1", self.prefs.get("u1", {}).get("preferred_name")


def _onboarding():
    users = FakeUserManager()
    svc = OnboardingService(users)
    svc.start_onboarding("discord", "123")
    return svc, users


def _respond(svc, text):
    return run(svc.handle_onboarding_response("u1", "discord", "123", text))


def test_flow_advances_name_then_timezone_then_memory():
    svc, users = _onboarding()
    assert svc.onboarding_states["discord:123"] == "name"

    _respond(svc, "Dain")
    assert users.prefs["u1"]["preferred_name"] == "Dain"
    assert svc.onboarding_states["discord:123"] == "timezone"

    _respond(svc, "california")
    assert svc.onboarding_states["discord:123"] == "memory"


def test_timezone_is_resolved_and_stored():
    svc, users = _onboarding()
    _respond(svc, "Dain")
    _, reply = _respond(svc, "i'm in california")
    assert users.prefs["u1"]["timezone"] == "US/Pacific"
    assert "pacific" in reply.lower()


def test_unresolvable_timezone_does_not_block_onboarding():
    svc, users = _onboarding()
    _respond(svc, "Dain")
    _, reply = _respond(svc, "somewhere cozy")
    # no timezone stored, but we still advance and reassure the user
    assert "timezone" not in users.prefs["u1"]
    assert svc.onboarding_states["discord:123"] == "memory"
    assert "figure out your timezone" in reply.lower()
