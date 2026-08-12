"""observations + assessments (phase 3, the council layer)

the council's structured output: observations are a helper's evidence-tied
noticing (distinct from memories - never rendered as conversation);
assessments are edwin's scored artifacts (table lands now, the scorer that
writes rows arrives in phase 6).

Revision ID: e9d5b7c31a84
Revises: c7e2f4a91d58
Create Date: 2026-08-12
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'e9d5b7c31a84'
down_revision: Union[str, Sequence[str], None] = 'c7e2f4a91d58'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'observations',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_uuid', sa.String(),
                  sa.ForeignKey('users.uuid'), nullable=False),
        sa.Column('helper_id', sa.String(), nullable=False),
        sa.Column('room_uuid', sa.String(), nullable=True),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('content', sa.String(), nullable=False),
        sa.Column('evidence', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_observations_user_uuid', 'observations', ['user_uuid'])
    op.create_index('ix_observations_user_helper', 'observations',
                    ['user_uuid', 'helper_id'])
    op.create_index('ix_observations_user_created', 'observations',
                    ['user_uuid', 'created_at'])

    op.create_table(
        'assessments',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_uuid', sa.String(),
                  sa.ForeignKey('users.uuid'), nullable=False),
        sa.Column('helper_id', sa.String(), nullable=False),
        sa.Column('subject_type', sa.String(), nullable=False),
        sa.Column('subject_id', sa.String(), nullable=True),
        sa.Column('summary', sa.String(), nullable=False),
        sa.Column('detail', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_assessments_user_uuid', 'assessments', ['user_uuid'])


def downgrade() -> None:
    op.drop_index('ix_assessments_user_uuid', table_name='assessments')
    op.drop_table('assessments')
    op.drop_index('ix_observations_user_created', table_name='observations')
    op.drop_index('ix_observations_user_helper', table_name='observations')
    op.drop_index('ix_observations_user_uuid', table_name='observations')
    op.drop_table('observations')
