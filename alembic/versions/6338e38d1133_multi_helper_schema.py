"""multi helper schema

phase 1 of the v3 ensemble (see docs/V3_DESIGN.md sections 2 and 6): schema-only
groundwork so later phases are pure feature work.

- memories gains created_by (attribution - which helper saved it, always set)
  and visibility ('shared' = every helper's search/core-memory rendering can
  see it, 'private' = only created_by). existing rows backfill to
  created_by='chordial', visibility='shared' (the only helper/scope that
  existed before this migration).
- helper_states: per-(user, helper) relationship state - met/active/declined,
  plus the chosen identity (persona_name/persona_form) denormalized from the
  identity core memory for the director's cast list.
- usage_log/agent_traces gain a nullable helper_id for per-helper cost
  visibility; unset until later-phase writers start populating it.

all additive, zero-downtime, sqlite-compatible.

Revision ID: 6338e38d1133
Revises: ecce34c113d1
Create Date: 2026-07-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6338e38d1133'
down_revision: Union[str, Sequence[str], None] = 'ecce34c113d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # memories: attribution + shared/private visibility
    op.add_column('memories', sa.Column('created_by', sa.String(), nullable=True,
                                         server_default='chordial'))
    op.add_column('memories', sa.Column('visibility', sa.String(), nullable=True,
                                         server_default='shared'))
    # server_default backfills sqlite's existing rows on ADD COLUMN, but spell
    # it out explicitly so this migration is correct on any backend.
    op.execute("UPDATE memories SET created_by = 'chordial' WHERE created_by IS NULL")
    op.execute("UPDATE memories SET visibility = 'shared' WHERE visibility IS NULL")

    # helper_states: per-(user, helper) relationship state
    op.create_table('helper_states',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_uuid', sa.String(), nullable=False),
        sa.Column('helper_id', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=True),
        sa.Column('persona_name', sa.String(), nullable=True),
        sa.Column('persona_form', sa.String(), nullable=True),
        sa.Column('introduced_at', sa.DateTime(), nullable=True),
        sa.Column('disabled_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_uuid'], ['users.uuid'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_uuid', 'helper_id', name='uq_helper_state_user_helper'),
        sqlite_autoincrement=True
    )

    # per-helper cost visibility (writers start populating these in a later phase)
    op.add_column('usage_log', sa.Column('helper_id', sa.String(), nullable=True))
    op.add_column('agent_traces', sa.Column('helper_id', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('agent_traces', schema=None) as batch_op:
        batch_op.drop_column('helper_id')
    with op.batch_alter_table('usage_log', schema=None) as batch_op:
        batch_op.drop_column('helper_id')

    op.drop_table('helper_states')

    with op.batch_alter_table('memories', schema=None) as batch_op:
        batch_op.drop_column('visibility')
        batch_op.drop_column('created_by')
