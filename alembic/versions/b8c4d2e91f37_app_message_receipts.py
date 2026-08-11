"""app_message_receipts: idempotent chat submission for the app

a retried POST /api/v1/rooms/current/messages must replay the stored
response, never re-run the model turn. unique (device_id, client_message_uuid)
is the dedup key; a null response marks an in-flight claim.

Revision ID: b8c4d2e91f37
Revises: f3a9c1d27b45
Create Date: 2026-08-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b8c4d2e91f37'
down_revision: Union[str, Sequence[str], None] = 'f3a9c1d27b45'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'app_message_receipts',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('device_id', sa.Integer(),
                  sa.ForeignKey('devices.id'), nullable=False),
        sa.Column('user_uuid', sa.String(),
                  sa.ForeignKey('users.uuid'), nullable=False),
        sa.Column('client_message_uuid', sa.String(), nullable=False),
        sa.Column('response', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('device_id', 'client_message_uuid',
                            name='uq_app_receipts_device_msg'),
        sqlite_autoincrement=True,
    )
    op.create_index('ix_app_message_receipts_user_uuid',
                    'app_message_receipts', ['user_uuid'])


def downgrade() -> None:
    op.drop_index('ix_app_message_receipts_user_uuid',
                  table_name='app_message_receipts')
    op.drop_table('app_message_receipts')
