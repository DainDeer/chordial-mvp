from datetime import datetime
from typing import Optional
import re
import logging

import pytz

logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "UTC"


# freeform answers -> IANA timezone, for resolving what a user types during
# onboarding ("california", "pacific time", "PST"). multi-word keys are matched
# as substrings; single-word keys only as whole words (so "et" doesn't match
# "meeting"). US-leaning, plus the common international zones.
_TZ_ALIASES = {
    # --- US ---
    "pacific": "US/Pacific", "pst": "US/Pacific", "pdt": "US/Pacific",
    "california": "US/Pacific", "los angeles": "US/Pacific", "san francisco": "US/Pacific",
    "seattle": "US/Pacific", "portland": "US/Pacific", "washington state": "US/Pacific",
    "mountain": "US/Mountain", "mst": "US/Mountain", "mdt": "US/Mountain",
    "denver": "US/Mountain", "colorado": "US/Mountain", "utah": "US/Mountain",
    "arizona": "US/Arizona", "phoenix": "US/Arizona",
    "central": "US/Central", "cst": "US/Central", "cdt": "US/Central",
    "chicago": "US/Central", "texas": "US/Central", "austin": "US/Central", "dallas": "US/Central",
    "eastern": "US/Eastern", "est": "US/Eastern", "edt": "US/Eastern",
    "new york": "US/Eastern", "nyc": "US/Eastern", "boston": "US/Eastern",
    "florida": "US/Eastern", "miami": "US/Eastern", "atlanta": "US/Eastern",
    "hawaii": "US/Hawaii", "hst": "US/Hawaii", "alaska": "US/Alaska",
    # --- international ---
    "london": "Europe/London", "britain": "Europe/London", "england": "Europe/London",
    "gmt": "Europe/London", "bst": "Europe/London",
    "paris": "Europe/Paris", "france": "Europe/Paris",
    "berlin": "Europe/Berlin", "germany": "Europe/Berlin", "cet": "Europe/Berlin",
    "madrid": "Europe/Madrid", "rome": "Europe/Rome", "italy": "Europe/Rome",
    "amsterdam": "Europe/Amsterdam", "netherlands": "Europe/Amsterdam",
    "tokyo": "Asia/Tokyo", "japan": "Asia/Tokyo", "jst": "Asia/Tokyo",
    "sydney": "Australia/Sydney", "melbourne": "Australia/Melbourne",
    "india": "Asia/Kolkata", "ist": "Asia/Kolkata",
    "singapore": "Asia/Singapore", "hong kong": "Asia/Hong_Kong",
    "toronto": "America/Toronto", "vancouver": "America/Vancouver",
    "utc": "UTC",
}

# pytz ships bare-abbreviation zones (EST, MST, HST, GMT, CET) that are fixed
# offset and DST-unaware. when a user types one of these we want the DST-aware
# region from the alias table instead, so we skip them in the exact-name match.
_LEGACY_ABBREV_ZONES = {"est", "mst", "hst", "gmt", "cet"}


def resolve_timezone(text: str) -> Optional[str]:
    """best-effort map a freeform answer to an IANA timezone name, or None if we
    can't tell. tries, in order: an exact IANA name (any case), then the alias
    table. used at onboarding so a user can say 'california' instead of having
    to know 'US/Pacific'; unresolved answers fall back to the chat flow."""
    if not text or not text.strip():
        return None
    raw = text.strip()
    lowered = raw.lower()

    # 1. an exact IANA name, case-insensitively ("US/Pacific", "america/new_york"),
    #    except the legacy bare-abbreviation zones we'd rather map DST-aware
    if lowered not in _LEGACY_ABBREV_ZONES:
        for tz in pytz.all_timezones:
            if tz.lower() == lowered:
                return tz

    # 2. alias table: multi-word keys as substrings, single-word keys as whole words
    tokens = set(re.findall(r"[a-z0-9+]+", lowered))
    for key, tz in _TZ_ALIASES.items():
        if " " in key:
            if key in lowered:
                return tz
        elif key in tokens:
            return tz

    return None


def utc_now() -> datetime:
    """naive utc timestamp, for storing/comparing against db timestamps"""
    return datetime.utcnow()


def _resolve_timezone(tz_name: str) -> pytz.BaseTzInfo:
    """look up a pytz timezone, falling back to utc for missing/invalid names"""
    if not tz_name:
        return pytz.UTC

    try:
        return pytz.timezone(tz_name)
    except pytz.UnknownTimeZoneError:
        logger.warning(f"unknown timezone '{tz_name}', falling back to UTC")
        return pytz.UTC


def to_user_timezone(dt_utc: datetime, tz_name: str) -> datetime:
    """
    convert a naive utc datetime into a naive local datetime for the user's timezone.

    stays naive on the way out so existing formatting code (which expects naive
    datetimes) doesn't need to change - it just receives values already shifted
    to the user's local time.
    """
    tz = _resolve_timezone(tz_name)
    aware_utc = pytz.UTC.localize(dt_utc)
    return aware_utc.astimezone(tz).replace(tzinfo=None)


def get_user_local_hour(dt_utc: datetime, tz_name: str) -> int:
    """get the hour (0-23) of the day in the user's local timezone"""
    return to_user_timezone(dt_utc, tz_name).hour


def is_within_quiet_hours(local_hour: int, quiet_start: int, quiet_end: int) -> bool:
    """
    check whether a local hour falls within a quiet-hours window.

    handles windows that wrap past midnight (e.g. start=21, end=8 means
    quiet from 9pm through 8am) as well as windows that don't (e.g.
    start=1, end=5 means quiet only between 1am and 5am).
    """
    if quiet_start == quiet_end:
        # zero-width window - never quiet
        return False

    if quiet_start > quiet_end:
        # wraps past midnight, e.g. 21 -> 8
        return local_hour >= quiet_start or local_hour < quiet_end

    # same-day window, e.g. 1 -> 5
    return quiet_start <= local_hour < quiet_end
