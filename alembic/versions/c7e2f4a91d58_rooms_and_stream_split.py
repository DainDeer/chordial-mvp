"""rooms + the row-level stream split (phase 2b)

conversation_events gains stream_id (WHICH conversation), backfilled to
user_uuid so all pre-rooms history becomes the grandfathered legacy room's
stream. rooms + room_summaries carry the section-4 lifecycle.

Revision ID: c7e2f4a91d58
Revises: b8c4d2e91f37
Create Date: 2026-08-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c7e2f4a91d58'
down_revision: Union[str, Sequence[str], None] = 'b8c4d2e91f37'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('conversation_events',
                  sa.Column('stream_id', sa.String(), nullable=True))
    # every existing event belongs to the user's legacy stream (whose id IS
    # the user uuid) - one statement, no chunking needed at current scale
    op.execute("UPDATE conversation_events SET stream_id = user_uuid "
               "WHERE stream_id IS NULL")
    op.create_index('ix_conversation_events_stream_id',
                    'conversation_events', ['stream_id'])

    op.create_table(
        'rooms',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('room_uuid', sa.String(), nullable=False),
        sa.Column('user_uuid', sa.String(),
                  sa.ForeignKey('users.uuid'), nullable=False),
        sa.Column('room_type', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False,
                  server_default='open'),
        sa.Column('date', sa.Date(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('closed_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('room_uuid'),
        sa.UniqueConstraint('user_uuid', 'room_type', 'date',
                            name='uq_rooms_user_type_date'),
        sqlite_autoincrement=True,
    )
    op.create_index('ix_rooms_user_uuid', 'rooms', ['user_uuid'])

    op.create_table(
        'room_summaries',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('room_id', sa.Integer(),
                  sa.ForeignKey('rooms.id'), nullable=False),
        sa.Column('user_uuid', sa.String(),
                  sa.ForeignKey('users.uuid'), nullable=False),
        sa.Column('content', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('room_id'),
        sqlite_autoincrement=True,
    )
    op.create_index('ix_room_summaries_user_uuid', 'room_summaries',
                    ['user_uuid'])


def downgrade() -> None:
    op.drop_index('ix_room_summaries_user_uuid', table_name='room_summaries')
    op.drop_table('room_summaries')
    op.drop_index('ix_rooms_user_uuid', table_name='rooms')
    op.drop_table('rooms')
    op.drop_index('ix_conversation_events_stream_id',
                  table_name='conversation_events')
    op.drop_column('conversation_events', 'stream_id')
