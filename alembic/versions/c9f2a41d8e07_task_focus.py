"""task_focus (the web focus view's per-task pomodoro clock)

one row per (user, task): banked seconds + a nullable running_since. guarded
with a reflection check so the upgrade is a no-op on databases born fresh via
create_all + stamp head (the postgres path from NATIVE_MIGRATION_PLAN §2.3).

Revision ID: c9f2a41d8e07
Revises: b3d41f9a72c8
Create Date: 2026-07-28

"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9f2a41d8e07'
down_revision: Union[str, Sequence[str], None] = 'b3d41f9a72c8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # the inspect guard covers divergent live installs; an offline render
    # follows the canonical chain, where the table doesn't exist yet
    if not context.is_offline_mode():
        bind = op.get_bind()
        if sa.inspect(bind).has_table('task_focus'):
            return
    op.create_table(
        'task_focus',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_uuid', sa.String(), nullable=False),
        sa.Column('task_id', sa.Integer(), nullable=False),
        sa.Column('accumulated_seconds', sa.Integer(), nullable=False,
                  server_default='0'),
        sa.Column('running_since', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_uuid'], ['users.uuid'], ),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_uuid', 'task_id',
                            name='uq_task_focus_user_task'),
        sqlite_autoincrement=True,
    )
    op.create_index(op.f('ix_task_focus_user_uuid'), 'task_focus',
                    ['user_uuid'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_task_focus_user_uuid'), table_name='task_focus')
    op.drop_table('task_focus')
