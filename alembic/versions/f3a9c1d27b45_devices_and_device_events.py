"""devices and device_events: app identity + the offline sync contract

phase 1 of the rooms overhaul (docs/ROOMS_DESIGN.md section 10): devices are
individually revocable credentials for the desktop app; device_events carry
the idempotency contract (unique event_uuid, unique (device_id, seq), durable
acked_seq cursor on the device row).

Revision ID: f3a9c1d27b45
Revises: a7c31e90f4d2
Create Date: 2026-08-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'f3a9c1d27b45'
down_revision: Union[str, Sequence[str], None] = 'a7c31e90f4d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'devices',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('device_uuid', sa.String(), nullable=False),
        sa.Column('user_uuid', sa.String(),
                  sa.ForeignKey('users.uuid'), nullable=False),
        sa.Column('name', sa.String(), nullable=False,
                  server_default='device'),
        sa.Column('token_hash', sa.String(), nullable=False),
        sa.Column('acked_seq', sa.Integer(), nullable=False,
                  server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(), nullable=True),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('device_uuid'),
        sqlite_autoincrement=True,
    )
    op.create_index('ix_devices_user_uuid', 'devices', ['user_uuid'])

    op.create_table(
        'device_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('event_uuid', sa.String(), nullable=False),
        sa.Column('device_id', sa.Integer(),
                  sa.ForeignKey('devices.id'), nullable=False),
        sa.Column('user_uuid', sa.String(),
                  sa.ForeignKey('users.uuid'), nullable=False),
        sa.Column('seq', sa.Integer(), nullable=False),
        sa.Column('event_type', sa.String(), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=True),
        sa.Column('occurred_at', sa.DateTime(), nullable=True),
        sa.Column('applied_at', sa.DateTime(), nullable=False),
        sa.Column('rejected', sa.Boolean(), nullable=False,
                  server_default=sa.text('0')),
        sa.Column('error', sa.String(), nullable=True),
        sa.UniqueConstraint('event_uuid'),
        sa.UniqueConstraint('device_id', 'seq',
                            name='uq_device_events_device_seq'),
        sqlite_autoincrement=True,
    )
    op.create_index('ix_device_events_user_uuid', 'device_events',
                    ['user_uuid'])


def downgrade() -> None:
    op.drop_index('ix_device_events_user_uuid', table_name='device_events')
    op.drop_table('device_events')
    op.drop_index('ix_devices_user_uuid', table_name='devices')
    op.drop_table('devices')
