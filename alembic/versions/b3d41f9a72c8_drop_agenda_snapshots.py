"""drop agenda_snapshots (phase D: the notion snapshot cache is gone)

the native workspace agenda reads live from postgres, so the per-user cached
snapshot row has no consumer. guarded with a reflection check so the upgrade
is a no-op on databases born fresh after the model was removed (create_all
never made the table there).

Revision ID: b3d41f9a72c8
Revises: e6bdc8daa497
Create Date: 2026-07-25

"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3d41f9a72c8'
down_revision: Union[str, Sequence[str], None] = 'e6bdc8daa497'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # the inspect guard covers divergent live installs; an offline render
    # (sql generation, no connection) follows the canonical chain, where
    # the table exists at this point by construction
    if context.is_offline_mode():
        op.drop_table('agenda_snapshots')
        return
    bind = op.get_bind()
    if sa.inspect(bind).has_table('agenda_snapshots'):
        op.drop_table('agenda_snapshots')


def downgrade() -> None:
    """Downgrade schema."""
    op.create_table('agenda_snapshots',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_uuid', sa.String(), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=True),
    sa.Column('digest', sa.String(), nullable=True),
    sa.Column('refreshed_at', sa.DateTime(), nullable=True),
    sa.Column('is_stale', sa.Boolean(), nullable=True),
    sa.Column('last_error', sa.String(), nullable=True),
    sa.ForeignKeyConstraint(['user_uuid'], ['users.uuid'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_uuid'),
    sqlite_autoincrement=True
    )
