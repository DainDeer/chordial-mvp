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

from zoneinfo import ZoneInfo  # noqa: E402

from src.utils.timezone_utils import (  # noqa: E402
    canonicalize_timezone,
    resolve_timezone,
)


class TestResolveTimezone:
    def test_exact_iana_name_any_case(self):
        # legacy aliases are accepted as input but resolve to the canonical
        # zone - only names stdlib zoneinfo can resolve may be stored (a
        # stored 'US/Pacific' once made the pulse's quiet hours fail closed)
        assert resolve_timezone("US/Pacific") == "America/Los_Angeles"
        assert resolve_timezone("america/new_york") == "America/New_York"
        assert resolve_timezone("Europe/London") == "Europe/London"

    def test_abbreviations_map_dst_aware(self):
        # pytz has bare "EST"/"MST" (fixed offset) - we want the DST-aware region
        assert resolve_timezone("PST") == "America/Los_Angeles"
        assert resolve_timezone("est") == "America/New_York"
        assert resolve_timezone("mst") == "America/Denver"

    def test_cities_and_regions(self):
        assert resolve_timezone("california") == "America/Los_Angeles"
        assert resolve_timezone("New York") == "America/New_York"
        assert resolve_timezone("london") == "Europe/London"
        assert resolve_timezone("tokyo") == "Asia/Tokyo"

    def test_full_sentences(self):
        assert resolve_timezone("i'm in california") == "America/Los_Angeles"
        assert resolve_timezone("pacific time please") == "America/Los_Angeles"
        assert resolve_timezone("i live near new york") == "America/New_York"

    def test_unresolvable_returns_none(self):
        assert resolve_timezone("") is None
        assert resolve_timezone("   ") is None
        assert resolve_timezone("somewhere over the rainbow") is None

    def test_short_key_does_not_match_inside_a_word(self):
        # "est" must match as a whole word, not inside "interested"/"forest"
        assert resolve_timezone("i'm interested in the forest") is None


class TestCanonicalizeTimezone:
    def test_legacy_aliases_map_to_canonical(self):
        assert canonicalize_timezone("US/Pacific") == "America/Los_Angeles"
        assert canonicalize_timezone("US/Eastern") == "America/New_York"
        assert canonicalize_timezone("US/Hawaii") == "Pacific/Honolulu"

    def test_canonical_and_unknown_pass_through(self):
        assert canonicalize_timezone("America/Chicago") == "America/Chicago"
        assert canonicalize_timezone("UTC") == "UTC"
        assert canonicalize_timezone("not/a-zone") == "not/a-zone"

    def test_every_emitted_zone_resolves_under_stdlib_zoneinfo(self):
        """the whole point: anything resolve_timezone or canonicalize can
        emit must be resolvable by zoneinfo (what the dainframe pulse uses),
        with or without host backward-links."""
        from src.utils.timezone_utils import _LEGACY_TO_CANONICAL, _TZ_ALIASES
        for tz in set(_TZ_ALIASES.values()) | set(_LEGACY_TO_CANONICAL.values()):
            ZoneInfo(tz)  # raises if unresolvable
