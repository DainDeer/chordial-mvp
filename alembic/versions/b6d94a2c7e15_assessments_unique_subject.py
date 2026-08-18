"""assessments unique subject - the scorer's exactly-once floor (phase 6)

Revision ID: b6d94a2c7e15
Revises: e2b7f18c4a90
Create Date: 2026-08-18 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b6d94a2c7e15'
down_revision: Union[str, Sequence[str], None] = 'e2b7f18c4a90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # one assessment per (user, subject_type, subject_id): the cycle
    # scorer's hard guarantee that a subject is scored exactly once,
    # whatever races. null subject_ids stay exempt (sql null semantics).
    op.create_index('uq_assessments_subject', 'assessments',
                    ['user_uuid', 'subject_type', 'subject_id'],
                    unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('uq_assessments_subject', table_name='assessments')
