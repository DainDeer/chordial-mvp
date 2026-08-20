"""usage_log meter indexes (phase 7c: the meter, sol's round)

the meter made usage_log a read path: budget_verdict runs (user_uuid,
created_at) on every conversational turn and every pulse firing once the
knobs are on, and the ops report scans (created_at) windows. neither had
an index - every message would scan the whole growing ledger. the
composite serves the gateway, the single column serves the dashboard.

Revision ID: d4f8b2a6c917
Revises: c9e2a5f7d431
Create Date: 2026-08-20
"""
from alembic import op

revision = 'd4f8b2a6c917'
down_revision = 'c9e2a5f7d431'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index('ix_usage_log_user_created', 'usage_log',
                    ['user_uuid', 'created_at'])
    op.create_index('ix_usage_log_created', 'usage_log', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_usage_log_created', table_name='usage_log')
    op.drop_index('ix_usage_log_user_created', table_name='usage_log')
