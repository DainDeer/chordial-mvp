"""per-(user, helper) relationship state: has this helper been met, is it
enabled for this user, and what identity did it take on for them.

layer 2 of the persona system (see docs/V3_DESIGN.md section 2): the persona
CARD is a frozen archetype shared by every user; the STATE is per-user - the
name/form the user chose at introduction ('Ember the red panda'), and whether
this helper is active/declined/disabled for them. the director's cast for a
user is exactly their helpers with status='active'; onboarding walks a helper
from 'not_met' -> 'introducing' -> 'active'|'declined'.

stateless like the other managers - construct freely, one short session per
call. the chosen name/form are DENORMALIZED here from the identity core memory
so the director's cast list is a plain column read, not a memory search.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from src.database.database import get_db
from src.database.models import HelperState
from src.utils.timezone_utils import utc_now

# the lifecycle a (user, helper) pair moves through.
STATUS_NOT_MET = "not_met"        # exists implicitly; never introduced
STATUS_INTRODUCING = "introducing"  # mid-introduction (a helper is meeting them)
STATUS_ACTIVE = "active"          # met + wanted: speaks, gets scheduled, is cast
STATUS_DECLINED = "declined"      # met + not wanted: never speaks (re-meetable)
STATUS_DISABLED = "disabled"      # turned off after being active
_VALID_STATUS = {STATUS_NOT_MET, STATUS_INTRODUCING, STATUS_ACTIVE,
                 STATUS_DECLINED, STATUS_DISABLED}


@dataclass
class HelperStateView:
    """detached, session-safe snapshot of one helper_states row."""
    helper_id: str
    status: str
    persona_name: Optional[str] = None
    persona_form: Optional[str] = None
    introduced_at: Optional[datetime] = None

    @property
    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE


class HelperStateManager:
    def _get_row(self, db, user_uuid: str, helper_id: str) -> Optional[HelperState]:
        return db.query(HelperState).filter(
            HelperState.user_uuid == user_uuid,
            HelperState.helper_id == helper_id,
        ).first()

    async def get(self, user_uuid: str, helper_id: str) -> HelperStateView:
        """this user's state for one helper. a pair with no row yet is
        'not_met' - the absence of a row IS the not-met state, so callers never
        have to special-case None."""
        with get_db() as db:
            row = self._get_row(db, user_uuid, helper_id)
            if row is None:
                return HelperStateView(helper_id=helper_id, status=STATUS_NOT_MET)
            return HelperStateView(
                helper_id=row.helper_id,
                status=row.status or STATUS_NOT_MET,
                persona_name=row.persona_name,
                persona_form=row.persona_form,
                introduced_at=row.introduced_at,
            )

    async def active_helpers(self, user_uuid: str) -> List[HelperStateView]:
        """every helper this user has active, in id order - the director's cast.
        chordial is the front door and is treated as active even without a row
        (a user always has the generalist), so callers can rely on a non-empty
        cast from the first message."""
        with get_db() as db:
            rows = db.query(HelperState).filter(
                HelperState.user_uuid == user_uuid,
                HelperState.status == STATUS_ACTIVE,
            ).order_by(HelperState.id).all()
            views = [
                HelperStateView(
                    helper_id=r.helper_id, status=r.status,
                    persona_name=r.persona_name, persona_form=r.persona_form,
                    introduced_at=r.introduced_at,
                ) for r in rows
            ]
        if not any(v.helper_id == "chordial" for v in views):
            views.insert(0, HelperStateView(helper_id="chordial", status=STATUS_ACTIVE))
        return views

    async def set_status(self, user_uuid: str, helper_id: str, status: str) -> None:
        """move a (user, helper) pair to a new lifecycle status, upserting the
        row. stamps introduced_at the first time a helper becomes active,
        disabled_at whenever it's disabled."""
        if status not in _VALID_STATUS:
            raise ValueError(f"invalid helper status '{status}' (one of {_VALID_STATUS})")
        with get_db() as db:
            row = self._get_row(db, user_uuid, helper_id)
            if row is None:
                row = HelperState(user_uuid=user_uuid, helper_id=helper_id)
                db.add(row)
            row.status = status
            if status == STATUS_ACTIVE and row.introduced_at is None:
                row.introduced_at = utc_now()
            if status == STATUS_DISABLED:
                row.disabled_at = utc_now()
            db.commit()

    async def set_identity(
        self, user_uuid: str, helper_id: str,
        persona_name: Optional[str], persona_form: Optional[str],
    ) -> None:
        """record the name/form the user chose for this helper (denormalized
        from the identity core memory). upserts the row without disturbing an
        existing status - identity can be re-chosen without re-introducing."""
        with get_db() as db:
            row = self._get_row(db, user_uuid, helper_id)
            if row is None:
                row = HelperState(user_uuid=user_uuid, helper_id=helper_id)
                db.add(row)
            row.persona_name = persona_name
            row.persona_form = persona_form
            db.commit()

    async def complete_introduction(
        self, user_uuid: str, helper_id: str, *, accepted: bool,
        persona_name: Optional[str] = None, persona_form: Optional[str] = None,
    ) -> None:
        """the atomic close of an introduction: set the chosen identity and land
        the pair in its final status (active if wanted, declined if not) in one
        write. the complete_introduction tool calls exactly this."""
        with get_db() as db:
            row = self._get_row(db, user_uuid, helper_id)
            if row is None:
                row = HelperState(user_uuid=user_uuid, helper_id=helper_id)
                db.add(row)
            row.persona_name = persona_name
            row.persona_form = persona_form
            row.status = STATUS_ACTIVE if accepted else STATUS_DECLINED
            if accepted and row.introduced_at is None:
                row.introduced_at = utc_now()
            db.commit()

    async def names_for(self, user_uuid: str) -> dict:
        """{helper_id: chosen_name} for helpers this user has named - the
        director renders its cast list with these so '@ember' / 'ask ember'
        resolves to chordial."""
        with get_db() as db:
            rows = db.query(HelperState).filter(
                HelperState.user_uuid == user_uuid,
                HelperState.persona_name.isnot(None),
            ).all()
            return {r.helper_id: r.persona_name for r in rows}
