"""the meter (phase 7c): per-user token budgets + the usage report.

the ledger has existed since v3 (usage_log: one row per model call,
user/helper/role/model attributed) - this module is the first thing that
READS it. two consumers:

- budgets: the ROOMS_DESIGN §10 promise ("per-user quotas and token
  budgets are enforced at the gateway") made real. spend is measured over
  a TRAILING 24h window - same rolling shape as SYNC_DAILY_EVENT_CAP, no
  midnight cliff to time a burst against, and recovery is gradual rather
  than a stampede at 00:00. two thresholds, both env knobs, 0 = off
  (single-user rigs stay unmetered by default - byte-identical behavior):

    soft (USER_TOKEN_BUDGET_SOFT_24H): proactive outreach stops. the
        council's cheapest lever - it was going to speak unprompted, now
        it waits. the user notices nothing except quiet.
    hard (USER_TOKEN_BUDGET_HARD_24H): conversational turns are refused
        with honest ephemeral copy (chat_service.BUDGET_REPLY). clocks,
        sync, and every non-model surface keep working.

  budgeted tokens = input + output. cache reads/writes are surfaced in
  the report but deliberately don't count - a budget that punished cache
  hits would punish exactly the architecture that keeps the council
  affordable. background utility work (curator, scorer) is bounded and
  rare and stays exempt; the decider rides the conversational turn it
  serves, so the hard gate already covers it.

- the report: usage_report() aggregates the ledger for the operator
  dashboard (/ops, gated by OPS_TOKEN). days are UTC calendar days -
  an operator view spanning users has no single "local" - and the
  per-user budget column uses the same trailing-24h arithmetic the
  gates enforce, so the dashboard never disagrees with the behavior.

every read here is advisory-fast and failure-open at the call sites: a
broken meter must never silence a check-in or a conversation.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func

from config import Config
from src.database.database import get_db
from src.database.models import UsageLog, User
from src.utils.timezone_utils import utc_now

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Spend:
    """one user's trailing-24h ledger sums."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    calls: int = 0

    @property
    def budgeted(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class BudgetVerdict:
    """where a user stands against the knobs. state is 'clear' | 'soft' |
    'hard'; a hard breach implies the soft consequences too (outreach
    checks `state != 'clear'`, conversation checks `state == 'hard'`)."""
    state: str
    spent: int
    soft: int
    hard: int


def spend_last_24h(user_uuid: str, now: Optional[datetime] = None) -> Spend:
    """ledger sums for the trailing 24h window (naive-utc, like the rows)."""
    since = (now or utc_now()) - timedelta(hours=24)
    with get_db() as db:
        row = db.query(
            func.coalesce(func.sum(UsageLog.input_tokens), 0),
            func.coalesce(func.sum(UsageLog.output_tokens), 0),
            func.coalesce(func.sum(UsageLog.cache_read_tokens), 0),
            func.coalesce(func.sum(UsageLog.cache_write_tokens), 0),
            func.count(UsageLog.id),
        ).filter(
            UsageLog.user_uuid == user_uuid,
            UsageLog.created_at >= since,
        ).one()
    return Spend(input_tokens=row[0], output_tokens=row[1],
                 cache_read_tokens=row[2], cache_write_tokens=row[3],
                 calls=row[4])


def budget_verdict(user_uuid: str,
                   now: Optional[datetime] = None) -> BudgetVerdict:
    """the gate arithmetic. both knobs off = 'clear' without touching the
    database (the unmetered default costs zero queries per turn)."""
    soft = Config.USER_TOKEN_BUDGET_SOFT_24H
    hard = Config.USER_TOKEN_BUDGET_HARD_24H
    if soft <= 0 and hard <= 0:
        return BudgetVerdict(state="clear", spent=0, soft=soft, hard=hard)
    spent = spend_last_24h(user_uuid, now).budgeted
    if hard > 0 and spent >= hard:
        state = "hard"
    elif soft > 0 and spent >= soft:
        state = "soft"
    else:
        state = "clear"
    return BudgetVerdict(state=state, spent=spent, soft=soft, hard=hard)


class BudgetGate:
    """pulse gate (wrapped in ScheduledOnly by the stack): a user past the
    SOFT budget gets no proactive outreach. placed LAST in the stack -
    it's the only gate that reads the ledger, so the cheap denials
    (onboarding, quiet hours, backoff) should win first. failure-open:
    a broken meter never silences a check-in."""

    async def check(self, firing, events, now):
        # local import: dainframe types stay out of module scope so the
        # report/budget half imports clean anywhere (scripts, tests)
        from dainframe.pulse import GateDecision
        try:
            verdict = await asyncio.to_thread(
                budget_verdict, firing.key.stream_id)
        except Exception:
            logger.exception("budget read failed for %s; allowing",
                             firing.key.stream_id)
            return GateDecision(True, "budget unreadable - failing open")
        if verdict.state != "clear":
            return GateDecision(
                False,
                f"token budget spent ({verdict.spent} in 24h, "
                f"soft {verdict.soft})")
        return GateDecision(True, "within budget")


def usage_report(days: int = 14, now: Optional[datetime] = None) -> dict:
    """the operator dashboard's payload: per-day, per-user, and per-model
    aggregates over the window, plus the budget knobs and each user's
    live trailing-24h standing. sync - callers to_thread it."""
    now = now or utc_now()
    since = now - timedelta(days=days)
    sums = (
        func.coalesce(func.sum(UsageLog.input_tokens), 0),
        func.coalesce(func.sum(UsageLog.output_tokens), 0),
        func.coalesce(func.sum(UsageLog.cache_read_tokens), 0),
        func.coalesce(func.sum(UsageLog.cache_write_tokens), 0),
        func.count(UsageLog.id),
    )

    def _tok(row, offset: int = 0) -> dict:
        return {"input": row[offset], "output": row[offset + 1],
                "cache_read": row[offset + 2], "cache_write": row[offset + 3],
                "calls": row[offset + 4]}

    with get_db() as db:
        day_col = func.date(UsageLog.created_at)
        by_day = [
            {"date": str(row[0]), **_tok(row, 1)}
            for row in db.query(day_col, *sums)
            .filter(UsageLog.created_at >= since)
            .group_by(day_col).order_by(day_col.desc()).all()
        ]
        by_role = [
            {"role": row[0] or "?", **_tok(row, 1)}
            for row in db.query(UsageLog.role, *sums)
            .filter(UsageLog.created_at >= since)
            .group_by(UsageLog.role).all()
        ]
        by_model = [
            {"model": row[0] or "?", **_tok(row, 1)}
            for row in db.query(UsageLog.model, *sums)
            .filter(UsageLog.created_at >= since)
            .group_by(UsageLog.model).all()
        ]
        user_rows = (
            db.query(UsageLog.user_uuid, *sums,
                     func.max(UsageLog.created_at))
            .filter(UsageLog.created_at >= since)
            .group_by(UsageLog.user_uuid).all()
        )
        names = dict(
            db.query(User.uuid, User.preferred_name)
            .filter(User.uuid.in_([r[0] for r in user_rows if r[0]]))
            .all()) if user_rows else {}

    users = []
    for row in user_rows:
        uuid = row[0]
        verdict = (budget_verdict(uuid, now) if uuid
                   else BudgetVerdict("clear", 0, 0, 0))
        users.append({
            "user_uuid": uuid,
            "name": names.get(uuid) if uuid else None,
            **_tok(row, 1),
            "last_call_at": row[6].isoformat() + "Z" if row[6] else None,
            "spent_24h": verdict.spent,
            "budget_state": verdict.state,
        })
    users.sort(key=lambda u: u["input"] + u["output"], reverse=True)

    return {
        "generated_at": now.isoformat() + "Z",
        "days": days,
        "budgets": {"soft": Config.USER_TOKEN_BUDGET_SOFT_24H,
                    "hard": Config.USER_TOKEN_BUDGET_HARD_24H},
        "by_day": by_day,
        "by_role": by_role,
        "by_model": by_model,
        "users": users,
    }
