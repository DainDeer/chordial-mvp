"""idempotency receipts for app chat submission (see AppMessageReceipt).

claim -> run the turn -> store. a retry finds the stored response and replays
it byte-for-byte; a concurrent duplicate finds an in-flight claim and is told
to retry shortly; a claim orphaned by a crash is reclaimable once it's stale.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from sqlalchemy.exc import IntegrityError

from src.database.database import get_db
from src.database.models import AppMessageReceipt
from src.utils.timezone_utils import utc_now

logger = logging.getLogger(__name__)

# an in-flight claim older than this is presumed orphaned (the process died
# mid-turn before storing a response) and may be taken over by a retry
STALE_CLAIM_SECONDS = 300

# receipts only need to outlive client retry windows; anything older is noise
RETENTION_DAYS = 7


class ClaimOutcome:
    """what claiming a client message id means for the caller."""

    def __init__(self, status: str, response: Optional[list] = None):
        self.status = status      # 'claimed' | 'replay' | 'in_flight'
        self.response = response  # set for 'replay'


def claim(device_id: int, user_uuid: str, client_message_uuid: str) -> ClaimOutcome:
    """claim the right to run this message's turn, exactly once.

    'claimed'  - ours to run (fresh claim, or a stale orphan taken over)
    'replay'   - already ran; response rides along for byte-identical replay
    'in_flight'- another request is running it right now; retry shortly
    """
    now = utc_now()
    with get_db() as db:
        # opportunistic hygiene: receipts past every retry window
        db.query(AppMessageReceipt).filter(
            AppMessageReceipt.user_uuid == user_uuid,
            AppMessageReceipt.created_at < now - timedelta(days=RETENTION_DAYS),
        ).delete(synchronize_session=False)

        try:
            with db.begin_nested():
                db.add(AppMessageReceipt(
                    device_id=device_id,
                    user_uuid=user_uuid,
                    client_message_uuid=client_message_uuid,
                    created_at=now,
                ))
            db.commit()
            return ClaimOutcome("claimed")
        except IntegrityError:
            pass

        row = db.query(AppMessageReceipt).filter(
            AppMessageReceipt.device_id == device_id,
            AppMessageReceipt.client_message_uuid == client_message_uuid,
        ).one()
        if row.response is not None:
            return ClaimOutcome("replay", response=row.response)
        if (now - row.created_at).total_seconds() > STALE_CLAIM_SECONDS:
            # orphaned mid-turn; take it over
            row.created_at = now
            db.commit()
            logger.warning("reclaimed stale app-message claim %s (device %s)",
                           client_message_uuid, device_id)
            return ClaimOutcome("claimed")
        return ClaimOutcome("in_flight")


def store(device_id: int, client_message_uuid: str, response: list) -> None:
    """store the delivered lines into the claim so retries replay them."""
    with get_db() as db:
        db.query(AppMessageReceipt).filter(
            AppMessageReceipt.device_id == device_id,
            AppMessageReceipt.client_message_uuid == client_message_uuid,
        ).update({"response": response, "completed_at": utc_now()},
                 synchronize_session=False)
        db.commit()


def release(device_id: int, client_message_uuid: str) -> None:
    """drop an unfulfilled claim (the turn raised before any response
    existed) so an honest retry isn't stuck waiting out the stale window."""
    with get_db() as db:
        db.query(AppMessageReceipt).filter(
            AppMessageReceipt.device_id == device_id,
            AppMessageReceipt.client_message_uuid == client_message_uuid,
            AppMessageReceipt.response.is_(None),
        ).delete(synchronize_session=False)
        db.commit()
