"""edwin's scorer: the phase-6 machinery that files one assessment per
ended cycle (docs/ROOMS_DESIGN.md section 8).

the honesty contract, in build order:

- THE SCORES ARE ARITHMETIC. src/services/scorecard.py computes all five
  components from the projection and the session ledger; no model ever
  invents or adjusts a number. an assessment lands even when there is no
  utility model at all.
- THE MODEL WRITES ONLY PROSE - a short summary and up to a handful of
  findings - and every finding must cite evidence refs (commitment uuids,
  scope-change ids, observation ids, component names) that the validator
  resolves against the rows actually gathered. a claim pointing at nothing
  is structurally unrecordable: dropped and counted, never filed. (the
  cadenza scorer lesson: enforce the shape in the handler, not the prompt.)
- EXACTLY ONCE. one assessment per (user, 'cycle', public_id), guarded by
  a unique index plus an IntegrityError-swallowing insert - the focus_flow
  discipline. scoring is a moment: late-arriving events do not reopen it.
- THE LEDGER STARTS NOW. discovery only looks back
  CYCLE_SCORER_BACKFILL_DAYS - ancient completed cycles (whose data
  predates the projection) are left unscored rather than judged on
  evidence that was never collected.

the watcher runs as a supervised task beside the pulse (the rewind-tether
pattern): a slow sweep that finds ended-but-unassessed cycles and scores
them. edwin SPEAKING about a scorecard is 6b's retro room; this module
only reads and files, which is most of what edwin does anyway.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, time as dtime, timedelta
from typing import Optional

from dainframe.providers.types import AIRequest, ChatTurn, SystemBlock

from config import Config
from src.database.database import get_db
from src.database.models import (Assessment, Cycle, DeviceEvent, Observation,
                                 User)
from src.services import scorecard
from src.services.cycles import CycleStore
from src.services.usage_recorder import UsageRecorder
from src.services.workspace import vocab
from src.utils.timezone_utils import to_user_timezone, utc_now

logger = logging.getLogger(__name__)

SCORER_ID = "edwin"
SUBJECT_TYPE = "cycle"

_MAX_FINDINGS = 6
_SUMMARY_CAP = 600
_CLAIM_CAP = 300
_ACTION_CAP = 200
_OBSERVATION_LIMIT = 50
_OBSERVATION_CLIP = 240

# byte-stable: one job, json out, evidence or silence.
_SCORER_SYSTEM = (
    "you are the reviewer for one person's two-week work cycle. the five "
    "component scores were computed from the ledger and are FINAL - you "
    "never invent, restate, or adjust a number. your job is the prose: a "
    "short summary and at most a few findings worth keeping.\n"
    "rules:\n"
    "- judge the plan and the system, never the person. 'the estimate was "
    "40% low' is a finding; 'you failed' is not.\n"
    "- every finding must cite refs from the provided lists (cm:<uuid>, "
    "sc:<id>, ob:<id>, component:<name>). a claim you cannot pin to a ref "
    "does not get written.\n"
    "- simplifications count as findings: an estimate that landed, a "
    "routine that no longer needs attention, a commitment easier than "
    "feared.\n"
    "- fewer findings beat more. zero findings is a fine answer when the "
    "cycle was unremarkable.\n"
    "- write plainly and precisely. no cheer, no scolding.\n"
    "respond with ONLY a json object, no prose, no code fences:\n"
    '{"summary": "<2-3 plain sentences>", "findings": [{"claim": "...", '
    '"evidence": ["cm:...", "component:..."], "suggested_action": '
    '"<optional>"}]}'
)


class CycleScorer:
    """discovery + scoring + filing. constructed once at startup; the
    provider is the shared utility model and may be None (the scorecard
    still files, with a deterministic summary)."""

    def __init__(self, provider=None, provider_name: Optional[str] = None,
                 usage_recorder: Optional[UsageRecorder] = None,
                 cycle_store: Optional[CycleStore] = None,
                 clock=utc_now, max_tokens: int = 1024):
        self.provider = provider
        self.provider_name = provider_name
        self.usage = usage_recorder or UsageRecorder()
        self.cycles = cycle_store or CycleStore()
        self.clock = clock
        self.max_tokens = max_tokens

    # --- the watcher --------------------------------------------------------

    async def run(self) -> None:
        while True:
            await asyncio.sleep(Config.CYCLE_SCORER_SWEEP_SECONDS)
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("cycle scorer sweep hiccup; continuing")

    async def sweep_once(self) -> int:
        """one pass: find ended-but-unassessed cycles, score each. returns
        assessments filed."""
        due = await asyncio.to_thread(self.cycles_due)
        filed = 0
        for user_uuid, cycle_id in due:
            try:
                if await self.score_cycle(user_uuid, cycle_id) is not None:
                    filed += 1
            except Exception:
                logger.exception("scoring cycle %s for %s failed; continuing",
                                 cycle_id, user_uuid)
        return filed

    # --- discovery ----------------------------------------------------------

    def cycles_due(self) -> list[tuple[str, int]]:
        """cycles whose window is over and whose scorecard is not filed:
        status complete, or active with an end date behind the user's local
        today. 'upcoming' never ran, so it is never scored. the backfill
        window keeps history's pre-phase-6 cycles out of the ledger."""
        now = self.clock()
        horizon = now - timedelta(days=Config.CYCLE_SCORER_BACKFILL_DAYS)
        due: list[tuple[str, int]] = []
        with get_db() as db:
            rows = db.query(Cycle, User).join(
                User, User.uuid == Cycle.user_uuid,
            ).filter(
                Cycle.status.in_(("active", "complete")),
            ).all()
            for cycle, user in rows:
                tz_name = user.timezone or "UTC"
                local_today = to_user_timezone(now, tz_name).date()
                if cycle.status == "active":
                    if cycle.end_date is None or cycle.end_date >= local_today:
                        continue
                ended = cycle.closed_at
                if ended is None and cycle.end_date is not None:
                    ended = datetime.combine(cycle.end_date, dtime.min)
                if ended is None or ended < horizon:
                    continue
                subject_id = vocab.public_id("cycle", cycle.id)
                exists = db.query(Assessment.id).filter(
                    Assessment.user_uuid == cycle.user_uuid,
                    Assessment.subject_type == SUBJECT_TYPE,
                    Assessment.subject_id == subject_id,
                ).first()
                if exists is None:
                    due.append((cycle.user_uuid, cycle.id))
        return due

    # --- scoring ------------------------------------------------------------

    async def score_cycle(self, user_uuid: str,
                          cycle_id: int) -> Optional[dict]:
        """score one cycle and file the assessment. returns the filed
        assessment dict, or None when there was nothing to do (no such
        cycle, or another sweep already filed it)."""
        gathered = await asyncio.to_thread(self._gather, user_uuid, cycle_id)
        if gathered is None:
            return None
        summary, findings, dropped, source = await self._compose(gathered)
        detail = {
            "components": gathered["card"]["components"],
            "numbers": gathered["card"]["numbers"],
            "findings": findings,
            "dropped_findings": dropped,
            "summary_source": source,
            "cycle": gathered["cycle_brief"],
        }
        filed = await asyncio.to_thread(
            self._file, user_uuid, gathered["subject_id"], summary, detail)
        if not filed:
            return None
        logger.info("edwin filed a scorecard for %s (%s findings, %s)",
                    gathered["subject_id"], len(findings), source)
        return {"subject_id": gathered["subject_id"], "summary": summary,
                "detail": detail}

    def _gather(self, user_uuid: str, cycle_id: int) -> Optional[dict]:
        """everything scoring needs, in one sync pass: the projection, the
        session slices on the user's local calendar, the observation pool,
        and the evidence-ref universe findings may cite."""
        projection = self.cycles.projection(user_uuid, cycle_id)
        if projection is None:
            return None
        subject_id = projection["cycle"]["public_id"]
        now = self.clock()
        with get_db() as db:
            exists = db.query(Assessment.id).filter(
                Assessment.user_uuid == user_uuid,
                Assessment.subject_type == SUBJECT_TYPE,
                Assessment.subject_id == subject_id,
            ).first()
            if exists is not None:
                return None
            user = db.query(User).filter(User.uuid == user_uuid).first()
            tz_name = (user.timezone if user and user.timezone else "UTC")
            sessions = self._session_slices(db, user_uuid,
                                            projection["cycle"], tz_name)
            observations = self._observation_pool(db, user_uuid,
                                                  projection["cycle"])
        today = to_user_timezone(now, tz_name).date()
        card = scorecard.compute_scorecard(
            projection, sessions, today=today,
            block_seconds=Config.POM_MINUTES * 60)
        allowed = {f"component:{name}" for name in scorecard.COMPONENTS}
        allowed.update(f"cm:{c['uuid']}" for c in projection["commitments"])
        allowed.update(f"sc:{s['id']}" for s in projection["scope_changes"])
        allowed.update(f"ob:{o['id']}" for o in observations)
        cycle = projection["cycle"]
        return {
            "user_uuid": user_uuid,
            "projection": projection,
            "card": card,
            "observations": observations,
            "allowed_refs": allowed,
            "subject_id": subject_id,
            "cycle_brief": {
                "id": cycle["id"], "public_id": cycle["public_id"],
                "title": cycle["title"], "theme": cycle["theme"],
                "status": cycle["status"], "start_date": cycle["start_date"],
                "end_date": cycle["end_date"],
                "frozen": projection["frozen"],
            },
        }

    @staticmethod
    def _session_slices(db, user_uuid: str, cycle: dict,
                        tz_name: str) -> list[scorecard.SessionSlice]:
        """session.ended events inside the cycle window, converted to the
        user's local calendar - the same coarse-sql / exact-local-check
        convention as the projection's progress pass."""
        start = scorecard._parse_date(cycle.get("start_date"))
        end = scorecard._parse_date(cycle.get("end_date"))
        q = db.query(DeviceEvent).filter(
            DeviceEvent.user_uuid == user_uuid,
            DeviceEvent.rejected.is_(False),
            DeviceEvent.event_type == "session.ended",
        )
        if start is not None:
            q = q.filter(DeviceEvent.occurred_at
                         >= datetime.combine(start - timedelta(days=1),
                                             dtime.min))
        if end is not None:
            q = q.filter(DeviceEvent.occurred_at
                         < datetime.combine(end + timedelta(days=2),
                                            dtime.min))
        slices = []
        for ev in q.all():
            payload = ev.payload or {}
            secs = payload.get("seconds")
            if not isinstance(secs, (int, float)) or isinstance(secs, bool) \
                    or secs <= 0:
                continue
            when = ev.occurred_at or ev.applied_at
            if when is None:
                continue
            local = to_user_timezone(when, tz_name)
            if start is not None and local.date() < start:
                continue
            if end is not None and local.date() > end:
                continue
            slices.append(scorecard.SessionSlice(
                seconds=int(secs), local_date=local.date(),
                local_hour=local.hour))
        return slices

    @staticmethod
    def _observation_pool(db, user_uuid: str, cycle: dict) -> list[dict]:
        """the council's noticings from the cycle window - the qualitative
        half of the evidence. coarse utc bounds (a day of slack each side)
        are fine here: an observation is context, not arithmetic."""
        start = scorecard._parse_date(cycle.get("start_date"))
        end = scorecard._parse_date(cycle.get("end_date"))
        q = db.query(Observation).filter(
            Observation.user_uuid == user_uuid)
        if start is not None:
            q = q.filter(Observation.created_at
                         >= datetime.combine(start - timedelta(days=1),
                                             dtime.min))
        if end is not None:
            q = q.filter(Observation.created_at
                         < datetime.combine(end + timedelta(days=2),
                                            dtime.min))
        rows = q.order_by(Observation.id.desc()).limit(
            _OBSERVATION_LIMIT).all()
        return [{"id": r.id, "helper_id": r.helper_id, "kind": r.kind,
                 "content": r.content} for r in rows]

    # --- the prose pass -----------------------------------------------------

    async def _compose(self, gathered: dict):
        """(summary, findings, dropped_count, source). the model path can
        fail in any way it likes - the deterministic summary files either
        way, because a scorecard with plain prose beats no scorecard."""
        if self.provider is None:
            return self._fallback_summary(gathered), [], 0, "deterministic"
        try:
            request = self._build_request(gathered)
            response = await self.provider.create_message(request)
            self.usage.record_call(
                user_uuid=self._user_of(gathered), platform=None,
                provider=self.provider_name, model=self.provider.model,
                role="scorer", usage=response.usage, helper_id=SCORER_ID,
            )
            summary, findings, dropped = self._parse(
                response.text, gathered["allowed_refs"])
        except Exception:
            logger.exception("scorer model pass failed for %s; filing "
                             "deterministic summary", gathered["subject_id"])
            return self._fallback_summary(gathered), [], 0, "deterministic"
        if summary is None:
            return self._fallback_summary(gathered), findings, dropped, \
                "deterministic"
        return summary, findings, dropped, "model"

    @staticmethod
    def _user_of(gathered: dict) -> str:
        # rides along from _gather for usage attribution only
        return gathered["user_uuid"]

    def _build_request(self, gathered: dict) -> AIRequest:
        card = gathered["card"]
        proj = gathered["projection"]
        lines = [f"cycle: {gathered['cycle_brief']['title']!r}, theme "
                 f"{gathered['cycle_brief']['theme']!r}, "
                 f"{gathered['cycle_brief']['start_date']} to "
                 f"{gathered['cycle_brief']['end_date']}",
                 "", "component scores (final):"]
        for name in scorecard.COMPONENTS:
            comp = card["components"][name]
            lines.append(f"- component:{name} = {comp['score']} "
                         f"({'; '.join(comp['evidence'])})")
        lines.append("")
        lines.append("commitments:")
        for c in proj["commitments"]:
            lines.append(
                f"- cm:{c['uuid']} {c['title']!r} priority={c['priority']} "
                f"status={c['status']} planned={c['blocks_planned']} "
                f"baseline={c['baseline_blocks']} done={c['blocks_done']}")
        if proj["scope_changes"]:
            lines.append("")
            lines.append("scope changes (the person's own words as reason):")
            for s in proj["scope_changes"]:
                lines.append(f"- sc:{s['id']} {s['deltas']} "
                             f"reason={s['reason']!r}")
        if gathered["observations"]:
            lines.append("")
            lines.append("council observations from the window:")
            for o in gathered["observations"]:
                content = " ".join((o["content"] or "").split())
                if len(content) > _OBSERVATION_CLIP:
                    content = content[:_OBSERVATION_CLIP - 1] + "…"
                lines.append(f"- ob:{o['id']} [{o['helper_id']}/{o['kind']}] "
                             f"{content}")
        return AIRequest(
            system=[SystemBlock(text=_SCORER_SYSTEM)],
            messages=[ChatTurn(role="user", content="\n".join(lines))],
            tools=[],
            max_tokens=self.max_tokens,
            effort=None,
        )

    @staticmethod
    def _parse(text: Optional[str], allowed_refs: set):
        """(summary|None, findings, dropped). the validator is the whole
        contract: a finding survives only when every one of its refs
        resolves against the gathered rows."""
        if not text:
            return None, [], 0
        raw = text.strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return None, [], 0
        try:
            data = json.loads(raw[start:end + 1])
        except (ValueError, TypeError):
            return None, [], 0
        if not isinstance(data, dict):
            return None, [], 0
        summary = data.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            summary = None
        else:
            summary = " ".join(summary.split())[:_SUMMARY_CAP]
        findings, dropped = [], 0
        raw_findings = data.get("findings")
        if isinstance(raw_findings, list):
            for item in raw_findings[:_MAX_FINDINGS]:
                cleaned = CycleScorer._clean_finding(item, allowed_refs)
                if cleaned is None:
                    dropped += 1
                else:
                    findings.append(cleaned)
        return summary, findings, dropped

    @staticmethod
    def _clean_finding(item, allowed_refs: set) -> Optional[dict]:
        if not isinstance(item, dict):
            return None
        claim = item.get("claim")
        if not isinstance(claim, str) or not claim.strip():
            return None
        evidence = item.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            return None
        refs = []
        for ref in evidence:
            if not isinstance(ref, str) or ref not in allowed_refs:
                return None
            refs.append(ref)
        cleaned = {"claim": " ".join(claim.split())[:_CLAIM_CAP],
                   "evidence": refs}
        action = item.get("suggested_action")
        if isinstance(action, str) and action.strip():
            cleaned["suggested_action"] = \
                " ".join(action.split())[:_ACTION_CAP]
        return cleaned

    @staticmethod
    def _fallback_summary(gathered: dict) -> str:
        """plain numbers, no judgment - what edwin would write if he only
        had the ledger and no ink."""
        numbers = gathered["card"]["numbers"]
        title = gathered["cycle_brief"]["title"]
        parts = [f"cycle {title!r} closed"]
        planned = numbers.get("planned_blocks")
        done = numbers.get("done_blocks")
        if planned:
            parts.append(f"{done} of {planned} planned blocks landed")
        elif done:
            parts.append(f"{done} blocks landed against no stated plan")
        active, elapsed = numbers.get("active_days"), numbers.get(
            "elapsed_days")
        if active is not None and elapsed:
            parts.append(f"work happened on {active} of {elapsed} days")
        return "; ".join(parts) + "."

    # --- filing -------------------------------------------------------------

    def _file(self, user_uuid: str, subject_id: str, summary: str,
              detail: dict) -> bool:
        """exactly-once insert: the unique (user, subject_type, subject_id)
        index is the hard floor; a lost race rolls back only the insert and
        reports not-filed."""
        from sqlalchemy.exc import IntegrityError
        with get_db() as db:
            try:
                with db.begin_nested():
                    db.add(Assessment(
                        user_uuid=user_uuid, helper_id=SCORER_ID,
                        subject_type=SUBJECT_TYPE, subject_id=subject_id,
                        summary=summary, detail=detail,
                        created_at=self.clock()))
            except IntegrityError:
                return False
            db.commit()
        return True


# --- read model --------------------------------------------------------------

def recent_assessments(user_uuid: str, *, subject_type: Optional[str] = None,
                       limit: int = 10) -> list[dict]:
    """newest-first assessments for one user - the tool layer and the api
    read model share this. plain dicts, detached."""
    limit = max(1, min(int(limit), 50))
    with get_db() as db:
        q = db.query(Assessment).filter(Assessment.user_uuid == user_uuid)
        if subject_type is not None:
            q = q.filter(Assessment.subject_type == subject_type)
        rows = q.order_by(Assessment.id.desc()).limit(limit).all()
        return [{
            "id": r.id, "helper_id": r.helper_id,
            "subject_type": r.subject_type, "subject_id": r.subject_id,
            "summary": r.summary, "detail": r.detail,
            "created_at": (r.created_at.isoformat()
                           if r.created_at else None),
        } for r in rows]
