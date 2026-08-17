"""the rewind offer machine (docs/REWIND_DESIGN.md section 6): one
correction question per drift episode, minted when drift is detected,
surfaced as the deer's card/chip, resolved by the person - never by time.

lifecycle: open -> applied | kept, applied -> open again via undo. there
is no lapse state and no timer that destroys the question; a run with an
open offer freezes at its stopping transition (focus.py) and banks only
when the answer lands.

impact framing is derived, never persisted: candidates store boundaries,
and payload() computes "remove Xm" fresh on every render from the end of
the quiet (the return, the freeze, or now) - a question answered late
states today's truth.

events ride the outbox (rewind.offered / applied / kept / undone);
rewind.offered carries the contested LENGTH only - candidate timestamps
stay on this device (data minimization, sol's design review).
"""
from __future__ import annotations

import uuid as uuid_mod
from datetime import datetime, timezone
from typing import Callable, Optional

from src.sidecar.rewind import (
    ActivityLedger,
    removal_impact,
    rewind_candidates,
)
from src.sidecar.store import SidecarStore


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


class OfferError(ValueError):
    """a request the offer state can't honor."""


class RewindOffers:
    def __init__(self, store: SidecarStore, ledger: ActivityLedger,
                 clock: Callable[[], datetime] = _utc_now):
        self.store = store
        self.ledger = ledger
        self.clock = clock

    # --- minting -----------------------------------------------------------

    def on_drift(self, run: dict) -> Optional[dict]:
        """drift detected on a running run: mint the question, or extend
        the run's open one (one unresolved correction per run). a REAL
        second episode - the previous quiet already ended in a return -
        closes that episode's interval into the offer's `segments`, so
        the single question accumulates every unresolved gap and 'remove'
        excises their union; re-detection of the same still-running quiet
        just recomputes candidates. returns the offer, or None when the
        ledger has nothing to ask (no candidates - e.g. a mid-run restart
        that witnessed nothing)."""
        now = self.clock()
        candidates = rewind_candidates(
            self.ledger.moments(), now, _parse(run["started_at"]),
            covered_from=self.ledger.covers_from)
        serialized = [{"at": c.at.isoformat(), "kind": c.kind,
                       "rank": c.rank} for c in candidates]
        existing = self.store.open_offer_for_run(run["id"])
        if existing is not None:
            if serialized:
                segments = list(existing["segments"])
                if existing.get("returned_at") and existing["candidates"]:
                    # the previous episode closed at its return: its
                    # interval (recommended boundary -> return) joins the
                    # question rather than being silently forgotten
                    segments.append([existing["candidates"][0]["at"],
                                     existing["returned_at"]])
                self.store.extend_offer(existing["offer_uuid"], serialized,
                                        segments)
                if len(segments) > len(existing["segments"]):
                    self._emit_offered(existing["offer_uuid"], run,
                                       candidates[0].at, now, segments)
            return self.store.get_offer(existing["offer_uuid"])
        if not serialized:
            return None
        offer_uuid = str(uuid_mod.uuid4())
        self.store.insert_offer(offer_uuid, run["id"], now.isoformat(),
                                serialized)
        self._emit_offered(offer_uuid, run, candidates[0].at, now, [])
        return self.store.get_offer(offer_uuid)

    def _emit_offered(self, offer_uuid: str, run: dict, boundary, now,
                      segments: list) -> None:
        """rewind.offered, (re-)emitted whenever the question's span
        grows - contested LENGTH only, boundaries stay on-device."""
        contested = (self._segment_seconds(segments)
                     + removal_impact(boundary, now))
        self.store.enqueue(
            "rewind.offered",
            {"offer_uuid": offer_uuid, "task_id": run["task_id"],
             "label": run["label"], "contested_seconds": contested},
            occurred_at=now.isoformat())

    def on_return(self, run: dict) -> Optional[dict]:
        """the person is back: stamp the end of the quiet (the excision
        interval's right edge, and the impact display's anchor - typing
        after the return must not inflate 'remove 17m'). returns the open
        offer for the card, or None."""
        offer = self.store.open_offer_for_run(run["id"])
        if offer is None:
            return None
        self.store.mark_offer_returned(offer["offer_uuid"],
                                       self.clock().isoformat())
        return self.store.get_offer(offer["offer_uuid"])

    # --- surfacing ----------------------------------------------------------

    def payload(self) -> Optional[dict]:
        """the newest open question, impact-framed for rendering - amounts
        derived NOW, never from when the offer was minted. includes the
        run's context so a chip can outlive a switch."""
        offer = self.store.newest_open_offer()
        if offer is None:
            return None
        run = self.store.run(offer["run_id"])
        if run is None or run["ended_at"] is not None:
            return None                      # banked runs are beyond reach
        # the removal anchors to the ends of the quiet stretches (typing
        # after a return never inflates "remove 17m"), but "clock becomes"
        # is live - credited keeps growing while the person works
        quiet_end = self._quiet_end(offer, run)
        credited_now = self._credited(run, self.clock())
        seg_total = self._segment_seconds(offer["segments"])
        candidates = []
        for c in offer["candidates"]:
            removed = seg_total + removal_impact(_parse(c["at"]), quiet_end)
            candidates.append({
                **c,
                "removed_seconds": removed,
                "credited_seconds": max(0, credited_now - removed),
            })
        return {
            "offer_uuid": offer["offer_uuid"],
            "run_id": offer["run_id"],
            "task_id": run["task_id"],
            "label": run["label"],
            "frozen": bool(run.get("frozen_at")),
            "frozen_reason": run.get("frozen_reason"),
            "contested_seconds": (candidates[0]["removed_seconds"]
                                  if candidates else 0),
            "candidates": candidates,
        }

    # --- resolving ----------------------------------------------------------

    def resolve(self, offer_uuid: str, action: str,
                at: Optional[str] = None, surface: str = "deer") -> dict:
        """the person answered. 'keep' credits everything; 'remove'
        excises [boundary, end-of-quiet], where the boundary must be one
        of the offer's own candidates (never an arbitrary time - that's
        the deferred scrubber). live runs only: a banked run is beyond
        reach. returns the offer with the resolution applied."""
        offer = self.store.get_offer(offer_uuid)
        if offer is None:
            raise OfferError("no such offer")
        if offer["status"] != "open":
            raise OfferError("that question was already answered")
        run = self.store.run(offer["run_id"])
        if run is None or run["ended_at"] is not None:
            raise OfferError("that run is already banked - "
                             "corrections touch live clocks only")
        now = self.clock()
        resolution = {"action": action, "surface": surface,
                      "resolved_at": now.isoformat()}
        if action == "keep":
            self.store.resolve_offer(offer_uuid, "kept", resolution)
            self.store.enqueue(
                "rewind.kept",
                {"offer_uuid": offer_uuid, "surface": surface},
                occurred_at=now.isoformat())
            return self.store.get_offer(offer_uuid)
        if action != "remove":
            raise OfferError("action must be 'remove' or 'keep'")

        wanted = at or offer["candidates"][0]["at"]
        matched = next((c for c in offer["candidates"]
                        if _parse(c["at"]) == _parse(wanted)), None)
        if matched is None:
            raise OfferError("that boundary isn't one of this "
                             "question's candidates")
        boundary = _parse(matched["at"])
        end = max(boundary, self._quiet_end(offer, run))
        # the union: every closed segment plus the current episode - the
        # question covered all of them, so the answer does too
        intervals = list(offer["segments"])
        intervals.append([boundary.isoformat(), end.isoformat()])
        resolution["at"] = boundary.isoformat()
        self.store.resolve_offer(offer_uuid, "applied", resolution,
                                 excised=intervals)
        self.store.enqueue(
            "rewind.applied",
            {"offer_uuid": offer_uuid, "surface": surface,
             "removed_seconds": self._segment_seconds(intervals)},
            occurred_at=now.isoformat())
        return self.store.get_offer(offer_uuid)

    def undo(self, offer_uuid: str) -> dict:
        """lift an applied excision and re-open the question - the tap may
        have been the mistake. live runs only, same as applying."""
        offer = self.store.get_offer(offer_uuid)
        if offer is None:
            raise OfferError("no such offer")
        if offer["status"] != "applied":
            raise OfferError("nothing applied to undo")
        run = self.store.run(offer["run_id"])
        if run is None or run["ended_at"] is not None:
            raise OfferError("that run is already banked - "
                             "corrections touch live clocks only")
        restored = self._segment_seconds(offer.get("excised") or [])
        self.store.reopen_offer(offer_uuid)
        self.store.enqueue(
            "rewind.undone",
            {"offer_uuid": offer_uuid, "restored_seconds": restored},
            occurred_at=self.clock().isoformat())
        return self.store.get_offer(offer_uuid)

    # --- shared arithmetic ---------------------------------------------------

    @staticmethod
    def _segment_seconds(segments: list) -> int:
        total = 0.0
        for start, end in segments:
            total += max(0.0, (_parse(end) - _parse(start)).total_seconds())
        return int(total)

    def _quiet_end(self, offer: dict, run: dict) -> datetime:
        """where the contested quiet stops: the return if one happened,
        else the freeze instant, else now - clamped to the freeze so a
        late answer can never excise past the run's own end."""
        if offer.get("returned_at"):
            end = _parse(offer["returned_at"])
        elif run.get("frozen_at"):
            end = _parse(run["frozen_at"])
        else:
            end = self.clock()
        if run.get("frozen_at"):
            end = min(end, _parse(run["frozen_at"]))
        return end

    def _credited(self, run: dict, as_of: datetime) -> int:
        end = _parse(run["frozen_at"]) if run.get("frozen_at") else as_of
        gross = max(0, int((end - _parse(run["started_at"])).total_seconds()))
        return max(0, gross - self.store.excised_seconds(run["id"]))
