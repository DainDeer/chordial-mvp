"""link codes carry a purpose: platform_link vs web_login

the web focus view's login flow reuses the LinkCode mechanic (short-lived,
single-use, unambiguous alphabet) but redeems into a browser session, not a
platform bind. the purpose column keeps the two redemption paths from ever
accepting each other's codes.

Revision ID: d81f3c55ab21
Revises: c9f2a41d8e07
Create Date: 2026-07-31

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd81f3c55ab21'
down_revision: Union[str, Sequence[str], None] = 'c9f2a41d8e07'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('link_codes', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'purpose', sa.String(), nullable=False,
            server_default='platform_link'))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('link_codes', schema=None) as batch_op:
        batch_op.drop_column('purpose')
