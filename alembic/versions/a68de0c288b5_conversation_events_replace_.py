"""conversation events replace conversation history

the conversation store becomes an event log: author-attributed (user /
chordial / curator / future personas) and kind-tagged (message / action /
note), so agent tool actions can live in the same ordered stream as chat
messages and multiple personas can share a channel later.

existing conversation_history rows are copied in (ordered by old id, so fresh
autoincrement ids preserve the original sequence exactly), then the old table
is dropped. compressed_messages keeps its conversation_history_id column as a
plain integer - the fk to the retired table is removed.

NOTE: downgrade is best-effort and LOSSY - only kind='message' events survive
the trip back; action events have no home in the old schema. back up the db
file before deploying if you care about rollback.

Revision ID: a68de0c288b5
Revises: 9f0b92c27ce6
Create Date: 2026-07-08 13:26:50.536474

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a68de0c288b5'
down_revision: Union[str, Sequence[str], None] = '9f0b92c27ce6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# sqlite stores the old fk unnamed; a naming convention lets batch mode
# address (and drop) it deterministically
_NAMING = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}


def upgrade() -> None:
    """Upgrade schema."""
    # 1. the new event log
    op.create_table('conversation_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_uuid', sa.String(), nullable=False),
    sa.Column('platform', sa.String(), nullable=True),
    sa.Column('author_type', sa.String(), nullable=False),
    sa.Column('author', sa.String(), nullable=False),
    sa.Column('kind', sa.String(), nullable=False),
    sa.Column('content', sa.String(), nullable=False),
    sa.Column('message_type', sa.String(), nullable=True),
    sa.Column('event_metadata', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['user_uuid'], ['users.uuid'], ),
    sa.PrimaryKeyConstraint('id'),
    sqlite_autoincrement=True
    )
    with op.batch_alter_table('conversation_events', schema=None) as batch_op:
        batch_op.create_index('ix_conversation_events_user_platform_id', ['user_uuid', 'platform', 'id'], unique=False)

    # 2. carry the old history over as message events, in original order
    op.execute("""
        INSERT INTO conversation_events
            (user_uuid, platform, author_type, author, kind, content, message_type, event_metadata, created_at)
        SELECT user_uuid, platform,
               CASE role WHEN 'user' THEN 'user' ELSE 'agent' END,
               CASE role WHEN 'user' THEN 'user' ELSE 'chordial' END,
               'message', content, message_type, '{}', created_at
        FROM conversation_history ORDER BY id
    """)

    # 3. detach compressed_messages from the table we're about to drop
    #    (batch recreate needs the referenced table still present, so this
    #    runs before the drop)
    with op.batch_alter_table('compressed_messages', schema=None, naming_convention=_NAMING) as batch_op:
        batch_op.drop_constraint(
            'fk_compressed_messages_conversation_history_id_conversation_history',
            type_='foreignkey',
        )

    # 4. retire the old table
    op.drop_table('conversation_history')


def downgrade() -> None:
    """Downgrade schema (lossy: action events are dropped)."""
    op.create_table('conversation_history',
    sa.Column('id', sa.INTEGER(), nullable=False),
    sa.Column('user_uuid', sa.VARCHAR(), nullable=True),
    sa.Column('platform', sa.VARCHAR(), nullable=True),
    sa.Column('role', sa.VARCHAR(), nullable=True),
    sa.Column('content', sa.VARCHAR(), nullable=True),
    sa.Column('message_type', sa.VARCHAR(), nullable=True),
    sa.Column('created_at', sa.DATETIME(), nullable=True),
    sa.ForeignKeyConstraint(['user_uuid'], ['users.uuid'], ),
    sa.PrimaryKeyConstraint('id'),
    sqlite_autoincrement=True
    )
    op.execute("""
        INSERT INTO conversation_history
            (user_uuid, platform, role, content, message_type, created_at)
        SELECT user_uuid, platform,
               CASE author_type WHEN 'user' THEN 'user' ELSE 'assistant' END,
               content, message_type, created_at
        FROM conversation_events WHERE kind = 'message' ORDER BY id
    """)

    with op.batch_alter_table('compressed_messages', schema=None, naming_convention=_NAMING) as batch_op:
        batch_op.create_foreign_key(
            'fk_compressed_messages_conversation_history_id_conversation_history',
            'conversation_history', ['conversation_history_id'], ['id'],
        )

    with op.batch_alter_table('conversation_events', schema=None) as batch_op:
        batch_op.drop_index('ix_conversation_events_user_platform_id')

    op.drop_table('conversation_events')
