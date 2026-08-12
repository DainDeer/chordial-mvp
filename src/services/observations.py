"""the observation store: a helper's structured noticing, kept.

'helpers reason more than they speak' (ROOMS_DESIGN.md §2): an observation
is the durable trace of the reasoning - a pattern, progress, or concern,
tied to what actually happened. it is NOT a memory: memories are curated
facts about the person that render into prompts; observations feed reviews
and edwin's scorecards, and are never replayed as conversation.

stateless like the other stores - construct freely, one short session per
call, detached dicts out.
"""
from __future__ import annotations

import logging
from typing import Optional

from src.database.database import get_db
from src.database.models import Observation

logger = logging.getLogger(__name__)

# the vocabulary of noticing. deliberately small: a kind is a coarse shelf
# for review-time filtering, not a taxonomy to agonize over.
OBSERVATION_KINDS = ("pattern", "progress", "concern", "context")

_CONTENT_CAP = 2000


class ObservationStore:
    def record(
        self,
        user_uuid: str,
        helper_id: str,
        kind: str,
        content: str,
        *,
        room_uuid: Optional[str] = None,
        evidence: Optional[dict] = None,
    ) -> dict:
        """append one observation. kind must be in OBSERVATION_KINDS and
        content non-empty - the store enforces the invariants, callers
        translate."""
        if kind not in OBSERVATION_KINDS:
            raise ValueError(
                f"unknown observation kind '{kind}' "
                f"(one of: {', '.join(OBSERVATION_KINDS)})"
            )
        content = (content or "").strip()
        if not content:
            raise ValueError("an observation needs content")
        with get_db() as db:
            row = Observation(
                user_uuid=user_uuid,
                helper_id=helper_id,
                room_uuid=room_uuid,
                kind=kind,
                content=content[:_CONTENT_CAP],
                evidence=evidence,
            )
            db.add(row)
            db.commit()
            return self._detach(row)

    def recent(
        self,
        user_uuid: str,
        *,
        helper_id: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict]:
        """newest-first slice of one user's observations, optionally filtered
        by observer and/or kind."""
        limit = max(1, min(int(limit), 100))
        with get_db() as db:
            q = db.query(Observation).filter(
                Observation.user_uuid == user_uuid)
            if helper_id is not None:
                q = q.filter(Observation.helper_id == helper_id)
            if kind is not None:
                q = q.filter(Observation.kind == kind)
            rows = q.order_by(Observation.id.desc()).limit(limit).all()
            return [self._detach(r) for r in rows]

    @staticmethod
    def _detach(row: Observation) -> dict:
        return {
            "id": row.id,
            "user_uuid": row.user_uuid,
            "helper_id": row.helper_id,
            "room_uuid": row.room_uuid,
            "kind": row.kind,
            "content": row.content,
            "evidence": row.evidence,
            "created_at": row.created_at,
        }
