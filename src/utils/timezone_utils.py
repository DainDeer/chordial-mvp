from datetime import datetime
import logging

import pytz

logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "UTC"


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
