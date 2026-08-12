"""the cycle spine (phase 5, ROOMS_DESIGN section 6)

cycles gain their planning fields (theme, capacity in focus blocks);
commitments become first-class rows with stable uuids; freezing writes an
append-only baseline snapshot (one per cycle); scope changes append instead
of rewriting history. device_events gains processed_at so the focus-flow
processor can turn applied events into consequences exactly once.

Revision ID: a4c8e21f9b63
Revises: e9d5b7c31a84
Create Date: 2026-08-12
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a4c8e21f9b63'
down_revision: Union[str, Sequence[str], None] = 'e9d5b7c31a84'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('cycles', sa.Column('theme', sa.String(), nullable=True))
    op.add_column('cycles',
                  sa.Column('capacity_blocks', sa.Integer(), nullable=True))

    op.create_table(
        'commitments',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('uuid', sa.String(), nullable=False, unique=True),
        sa.Column('user_uuid', sa.String(),
                  sa.ForeignKey('users.uuid'), nullable=False),
        sa.Column('cycle_id', sa.Integer(),
                  sa.ForeignKey('cycles.id'), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('priority', sa.String(), nullable=True),
        sa.Column('blocks_planned', sa.Integer(), nullable=True),
        sa.Column('next_action', sa.String(), nullable=True),
        sa.Column('plan_id', sa.Integer(),
                  sa.ForeignKey('plans.id'), nullable=True),
        sa.Column('task_id', sa.Integer(),
                  sa.ForeignKey('tasks.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('closed_at', sa.DateTime(), nullable=True),
        sqlite_autoincrement=True,
    )
    op.create_index('ix_commitments_user_uuid', 'commitments', ['user_uuid'])
    op.create_index('ix_commitments_user_cycle', 'commitments',
                    ['user_uuid', 'cycle_id'])

    op.create_table(
        'cycle_baselines',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_uuid', sa.String(),
                  sa.ForeignKey('users.uuid'), nullable=False),
        sa.Column('cycle_id', sa.Integer(),
                  sa.ForeignKey('cycles.id'), nullable=False),
        sa.Column('snapshot', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('cycle_id', name='uq_cycle_baselines_cycle'),
        sqlite_autoincrement=True,
    )
    op.create_index('ix_cycle_baselines_user_uuid', 'cycle_baselines',
                    ['user_uuid'])

    op.create_table(
        'scope_changes',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_uuid', sa.String(),
                  sa.ForeignKey('users.uuid'), nullable=False),
        sa.Column('cycle_id', sa.Integer(),
                  sa.ForeignKey('cycles.id'), nullable=False),
        sa.Column('commitment_uuid', sa.String(), nullable=True),
        sa.Column('reason', sa.String(), nullable=False),
        sa.Column('deltas', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sqlite_autoincrement=True,
    )
    op.create_index('ix_scope_changes_user_uuid', 'scope_changes',
                    ['user_uuid'])
    op.create_index('ix_scope_changes_user_cycle', 'scope_changes',
                    ['user_uuid', 'cycle_id'])

    op.add_column('device_events',
                  sa.Column('processed_at', sa.DateTime(), nullable=True))
    op.create_index('ix_device_events_unprocessed', 'device_events',
                    ['user_uuid', 'processed_at'])


def downgrade() -> None:
    op.drop_index('ix_device_events_unprocessed', table_name='device_events')
    op.drop_column('device_events', 'processed_at')
    op.drop_index('ix_scope_changes_user_cycle', table_name='scope_changes')
    op.drop_index('ix_scope_changes_user_uuid', table_name='scope_changes')
    op.drop_table('scope_changes')
    op.drop_index('ix_cycle_baselines_user_uuid', table_name='cycle_baselines')
    op.drop_table('cycle_baselines')
    op.drop_index('ix_commitments_user_cycle', table_name='commitments')
    op.drop_index('ix_commitments_user_uuid', table_name='commitments')
    op.drop_table('commitments')
    op.drop_column('cycles', 'capacity_blocks')
    op.drop_column('cycles', 'theme')
