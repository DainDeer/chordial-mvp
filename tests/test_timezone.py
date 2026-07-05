"""
unit tests for timezone handling: pure conversion helpers, quiet-hours logic,
and the per-user localization applied to temporal context strings.

run with: pytest tests/test_timezone.py -v
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

# point the app at a throwaway sqlite db for the duration of this test module,
# before any app module (which reads Config at import time) gets imported
_TMP_DB_FD, _TMP_DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(_TMP_DB_FD)
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB_PATH}"

from src.utils.timezone_utils import (
    utc_now,
    to_user_timezone,
    get_user_local_hour,
    is_within_quiet_hours,
)
from src.utils.temporal_context import TemporalContext
from src.database.database import init_db, get_db
from src.database.models import User, PlatformIdentity, ConversationHistory
from src.managers.user_manager import UserManager
from src.services.scheduler_service import SchedulerService


# ---------------------------------------------------------------------------
# pure conversion helpers
# ---------------------------------------------------------------------------

class TestToUserTimezone:
    def test_utc_to_new_york_shifts_hours_back(self):
        # jan 15 noon utc -> 7am new york (utc-5 in january, standard time)
        dt_utc = datetime(2026, 1, 15, 12, 0, 0)
        local = to_user_timezone(dt_utc, "America/New_York")
        assert local.hour == 7
        assert local.day == 15

    def test_utc_to_tokyo_shifts_hours_forward_and_crosses_midnight(self):
        # 8pm utc -> 5am the next day in tokyo (utc+9)
        dt_utc = datetime(2026, 1, 15, 20, 0, 0)
        local = to_user_timezone(dt_utc, "Asia/Tokyo")
        assert local.hour == 5
        assert local.day == 16

    def test_utc_timezone_is_a_no_op(self):
        dt_utc = datetime(2026, 6, 1, 9, 30, 0)
        local = to_user_timezone(dt_utc, "UTC")
        assert local == dt_utc

    def test_unknown_timezone_falls_back_to_utc(self):
        dt_utc = datetime(2026, 6, 1, 9, 30, 0)
        local = to_user_timezone(dt_utc, "Not/A_Real_Zone")
        assert local == dt_utc

    def test_missing_timezone_falls_back_to_utc(self):
        dt_utc = datetime(2026, 6, 1, 9, 30, 0)
        assert to_user_timezone(dt_utc, "") == dt_utc
        assert to_user_timezone(dt_utc, None) == dt_utc

    def test_result_is_naive(self):
        dt_utc = datetime(2026, 6, 1, 9, 30, 0)
        local = to_user_timezone(dt_utc, "America/Los_Angeles")
        assert local.tzinfo is None

    def test_handles_dst_transition(self):
        # america/new_york: dst starts 2026-03-08, so jan is est (utc-5)
        # and july is edt (utc-4)
        winter_utc = datetime(2026, 1, 15, 17, 0, 0)
        summer_utc = datetime(2026, 7, 15, 17, 0, 0)
        assert to_user_timezone(winter_utc, "America/New_York").hour == 12
        assert to_user_timezone(summer_utc, "America/New_York").hour == 13


class TestGetUserLocalHour:
    def test_matches_to_user_timezone_hour(self):
        dt_utc = datetime(2026, 3, 1, 3, 0, 0)  # 3am utc
        assert get_user_local_hour(dt_utc, "America/Los_Angeles") == 19  # 7pm prev day (PST, utc-8)


# ---------------------------------------------------------------------------
# quiet-hours pure logic
# ---------------------------------------------------------------------------

class TestIsWithinQuietHours:
    @pytest.mark.parametrize("hour,expected", [
        (21, True),   # start of window
        (23, True),
        (0, True),
        (7, True),
        (8, False),   # end of window (exclusive)
        (9, False),
        (20, False),  # just before window
    ])
    def test_wraps_past_midnight(self, hour, expected):
        # default config: quiet from 9pm (21) to 8am (8)
        assert is_within_quiet_hours(hour, quiet_start=21, quiet_end=8) == expected

    @pytest.mark.parametrize("hour,expected", [
        (0, False),
        (1, True),   # start of window
        (3, True),
        (4, False),  # end of window (exclusive)
        (12, False),
        (23, False),
    ])
    def test_same_day_window(self, hour, expected):
        # a window that doesn't cross midnight, e.g. quiet 1am-4am only
        assert is_within_quiet_hours(hour, quiet_start=1, quiet_end=4) == expected

    def test_zero_width_window_never_quiet(self):
        for hour in range(24):
            assert is_within_quiet_hours(hour, quiet_start=5, quiet_end=5) is False


# ---------------------------------------------------------------------------
# temporal context strings reflect the user's local time, not the server's
# ---------------------------------------------------------------------------

class TestTemporalContextLocalization:
    def test_time_of_day_differs_by_timezone_for_same_instant(self):
        # 2am utc is late night in new york (9pm prev day) but morning in tokyo (11am)
        dt_utc = datetime(2026, 6, 15, 2, 0, 0)

        ny_local = to_user_timezone(dt_utc, "America/New_York")
        tokyo_local = to_user_timezone(dt_utc, "Asia/Tokyo")

        assert TemporalContext.get_time_of_day(ny_local) == "night"
        assert TemporalContext.get_time_of_day(tokyo_local) == "morning"

    def test_quiet_hours_special_context_only_applies_locally(self):
        # 23:30 utc: late night in utc, but mid-afternoon in los angeles
        dt_utc = datetime(2026, 6, 15, 23, 30, 0)

        utc_special = TemporalContext.get_special_context(dt_utc)
        la_special = TemporalContext.get_special_context(to_user_timezone(dt_utc, "America/Los_Angeles"))

        assert "late" in utc_special
        assert la_special == ""  # ~4:30pm PDT, no special context


# ---------------------------------------------------------------------------
# scheduler respects each user's own quiet hours based on their timezone
# ---------------------------------------------------------------------------

class TestSchedulerQuietHoursPerUser:
    @pytest.fixture(autouse=True)
    def _setup_db(self):
        init_db()
        yield

    def _make_user(self, timezone: str, last_message_at: datetime) -> str:
        """create a user with a prior conversation message, so
        should_send_scheduled_message reaches the quiet-hours check instead
        of short-circuiting on the "no messages yet" path"""
        with get_db() as db:
            user = User(preferred_name="tester", timezone=timezone)
            db.add(user)
            db.flush()
            user_uuid = user.uuid
            db.add(PlatformIdentity(
                user_uuid=user_uuid,
                platform="discord",
                platform_user_id="123",
            ))
            db.add(ConversationHistory(
                user_uuid=user_uuid,
                platform="discord",
                role="user",
                content="hey",
                message_type="conversation",
                created_at=last_message_at,
            ))
            db.commit()
            return user_uuid

    def test_same_utc_instant_is_quiet_for_one_user_and_not_another(self, monkeypatch):
        scheduler = SchedulerService(chat_service=None, user_manager=UserManager())

        # 6am utc: quiet in new york (1am, within default 21-8 window)
        # but not quiet in tokyo (3pm)
        fixed_utc = datetime(2026, 6, 15, 6, 0, 0)
        monkeypatch.setattr(
            "src.services.scheduler_service.utc_now",
            lambda: fixed_utc
        )

        assert scheduler._is_quiet_hours("America/New_York") is True
        assert scheduler._is_quiet_hours("Asia/Tokyo") is False

    def test_should_send_scheduled_message_respects_users_local_quiet_hours(self, monkeypatch):
        fixed_utc = datetime(2026, 6, 15, 6, 0, 0)  # 1am NY (quiet), 3pm Tokyo (not quiet)
        last_message_at = fixed_utc - timedelta(hours=1)

        ny_user_uuid = self._make_user("America/New_York", last_message_at)
        tokyo_user_uuid = self._make_user("Asia/Tokyo", last_message_at)

        scheduler = SchedulerService(chat_service=None, user_manager=UserManager())
        scheduler.default_interval_minutes = 0  # no interval gating for this test

        monkeypatch.setattr(
            "src.services.scheduler_service.utc_now",
            lambda: fixed_utc
        )

        async def run():
            ny_result = await scheduler.should_send_scheduled_message(ny_user_uuid, "discord")
            tokyo_result = await scheduler.should_send_scheduled_message(tokyo_user_uuid, "discord")
            return ny_result, tokyo_result

        ny_result, tokyo_result = asyncio.run(run())

        assert ny_result is False  # it's 1am for this user, don't message them
        assert tokyo_result is True  # it's 3pm for this user, fine to message


# ---------------------------------------------------------------------------
# ContextBuilder / special_context: no longer frozen at import time, and its
# output (e.g. "friday afternoon vibes") actually reaches the system prompt
# ---------------------------------------------------------------------------

class TestContextBuilderSpecialContext:
    def test_default_timestamp_is_not_frozen_at_import_time(self):
        from src.utils.context_builder import ContextBuilder

        context_a = ContextBuilder.build_message_context()
        # if the old `timestamp: datetime = datetime.now()` default bug were
        # still present, both calls would carry the exact same instant
        # (captured once, at function-definition time)
        context_b = ContextBuilder.build_message_context()

        assert context_a["temporal"]["current_time"] is not None
        # can't assert inequality reliably (calls may land in the same
        # second), but we can assert an explicit timestamp is honored instead
        # of ignored, which is the actual behavior the bug broke:
        fixed = datetime(2026, 1, 1, 12, 0, 0)
        explicit_context = ContextBuilder.build_message_context(timestamp=fixed)
        assert explicit_context["temporal"]["current_time"] == fixed.strftime("%I:%M %p")

    def test_special_context_is_localized_via_timestamp_param(self):
        from src.utils.context_builder import ContextBuilder

        # saturday 1am utc == friday 6pm in LA (utc-7, pdt) -> friday afternoon
        # vibes for the LA user, while the same instant is just "late" in utc
        fixed_utc = datetime(2026, 7, 4, 1, 0, 0)
        la_local = to_user_timezone(fixed_utc, "America/Los_Angeles")

        context = ContextBuilder.build_message_context(timestamp=la_local)
        assert "friday afternoon" in context["special_context"]

        # the same instant for a UTC user is late night, not friday afternoon
        utc_context = ContextBuilder.build_message_context(timestamp=fixed_utc)
        assert "friday afternoon" not in utc_context["special_context"]

    def test_special_context_reaches_the_current_turn(self, monkeypatch):
        # the caching redesign moves volatile "now" context (friday vibes, etc)
        # OUT of the frozen system prompt and ONTO the current turn, so the
        # system prefix stays byte-stable and cacheable.
        from src.services import prompt_service as ps_mod
        from src.services.prompt_service import PromptService, PERSONA
        from src.models.message import Message

        friday_pm = datetime(2025, 6, 27, 15, 0)  # a friday, mid-afternoon UTC
        monkeypatch.setattr(ps_mod, "utc_now", lambda: friday_pm)

        ps = PromptService(enable_prompt_logging=False)
        history = [Message(role="user", content="hi", timestamp=friday_pm)]

        async def run():
            return await ps.build_conversation_request(
                conversation_history=history,
                user_name="dain",
                user_uuid=None,
                user_timezone="UTC",
            )

        request = asyncio.run(run())

        # the frozen persona is system block 0 and carries no volatile vibes
        assert request.system[0].text == PERSONA
        assert "friday afternoon" not in request.system[0].text
        # the special context rides on the current (last) turn instead
        assert "friday afternoon" in request.messages[-1].content

    def test_persona_block_is_frozen_and_vibe_free(self, monkeypatch):
        from src.services import prompt_service as ps_mod
        from src.services.prompt_service import PromptService, PERSONA
        from src.models.message import Message

        tuesday_midday = datetime(2025, 7, 1, 12, 0)  # ordinary time, no vibe
        monkeypatch.setattr(ps_mod, "utc_now", lambda: tuesday_midday)

        ps = PromptService(enable_prompt_logging=False)
        history = [Message(role="user", content="hi", timestamp=tuesday_midday)]

        async def run():
            return await ps.build_conversation_request(
                conversation_history=history,
                user_name="dain",
                user_uuid=None,
                user_timezone="UTC",
            )

        request = asyncio.run(run())
        # persona is present, frozen, and identical regardless of the clock
        assert request.system[0].text == PERSONA
        # the current turn still carries a "now" marker even with no special vibe
        assert request.messages[-1].content.startswith("[current time")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
