"""`resolve_timezone`: freeform answer -> IANA name.

this used to also cover OnboardingService's name -> timezone -> memory state
machine; that service is retired (see docs/V3_DESIGN.md section 3 - the v1
onboarding overhaul) and its introduction-flow coverage now lives in
tests/test_introduction.py. `resolve_timezone` itself is still a real, useful
utility (the model calls set_preference with whatever freeform timezone text
the user gives it), so its unit coverage stays here.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.utils.timezone_utils import resolve_timezone  # noqa: E402


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
