"""rewind_decisions - the tether's pending-decision rows (REWIND_DESIGN §8)

Revision ID: e2b7f18c4a90
Revises: fa0005de11c0
Create Date: 2026-08-17 21:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e2b7f18c4a90'
down_revision: Union[str, Sequence[str], None] = 'fa0005de11c0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'rewind_decisions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_uuid', sa.String(), nullable=False),
        sa.Column('offer_uuid', sa.String(), nullable=False),
        sa.Column('label', sa.String(), nullable=True),
        sa.Column('contested_seconds', sa.Integer(), nullable=False,
                  server_default=sa.text('0')),
        sa.Column('offered_at', sa.DateTime(), nullable=False),
        sa.Column('returned_at', sa.DateTime(), nullable=True),
        sa.Column('pinged_at', sa.DateTime(), nullable=True),
        sa.Column('ping_platform', sa.String(), nullable=True),
        sa.Column('choice', sa.String(), nullable=True),
        sa.Column('decided_at', sa.DateTime(), nullable=True),
        sa.Column('source', sa.String(), nullable=True),
        sa.Column('closed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_uuid'], ['users.uuid']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('offer_uuid'),
    )
    op.create_index('ix_rewind_decisions_user_uuid', 'rewind_decisions',
                    ['user_uuid'])
    op.create_index('ix_rewind_decisions_open', 'rewind_decisions',
                    ['user_uuid', 'closed_at'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_rewind_decisions_open', table_name='rewind_decisions')
    op.drop_index('ix_rewind_decisions_user_uuid',
                  table_name='rewind_decisions')
    op.drop_table('rewind_decisions')
