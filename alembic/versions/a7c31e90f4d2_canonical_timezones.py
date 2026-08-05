"""stored timezones become canonical IANA names

pytz (which validates user input) accepts legacy aliases like 'US/Pacific',
but the dainframe pulse resolves zones with stdlib zoneinfo, which only knows
those aliases when the host tzdata ships backward-links. a stored legacy name
made quiet hours fail closed and silenced every proactive send. the code now
canonicalizes at every seam; this migration fixes the rows already written.

Revision ID: a7c31e90f4d2
Revises: d81f3c55ab21
Create Date: 2026-08-04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7c31e90f4d2'
down_revision: Union[str, Sequence[str], None] = 'd81f3c55ab21'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# mirrors src/utils/timezone_utils._LEGACY_TO_CANONICAL at the time of this
# migration (deliberately frozen here - migrations must not import app code)
_LEGACY_TO_CANONICAL = {
    "US/Pacific": "America/Los_Angeles",
    "US/Mountain": "America/Denver",
    "US/Arizona": "America/Phoenix",
    "US/Central": "America/Chicago",
    "US/Eastern": "America/New_York",
    "US/East-Indiana": "America/Indiana/Indianapolis",
    "US/Michigan": "America/Detroit",
    "US/Hawaii": "Pacific/Honolulu",
    "US/Alaska": "America/Anchorage",
    "US/Aleutian": "America/Adak",
    "US/Samoa": "Pacific/Pago_Pago",
    "GB": "Europe/London",
    "Eire": "Europe/Dublin",
    "Japan": "Asia/Tokyo",
    "NZ": "Pacific/Auckland",
}

users = sa.table('users', sa.column('timezone', sa.String))


def upgrade() -> None:
    """Upgrade schema."""
    for legacy, canonical in _LEGACY_TO_CANONICAL.items():
        op.execute(
            users.update()
            .where(users.c.timezone == op.inline_literal(legacy))
            .values(timezone=op.inline_literal(canonical))
        )


def downgrade() -> None:
    """Downgrade schema."""
    # canonical names resolve everywhere the legacy ones did; nothing to undo
    pass
