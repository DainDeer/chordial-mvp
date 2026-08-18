"""rooms subject anchor (phase 6b: the cycle rooms)

rooms gain subject_type/subject_id - the anchor that names what an undated
room is ABOUT (subject_type='cycle', subject_id='cy12', the assessments
vocabulary). the unique index is the exactly-once guard the dated
constraint can't give cycle rooms: null dates are distinct, so
uq_rooms_user_type_date never fires for them - (user, room_type,
subject_id) does. daily/legacy rows keep null subjects and are untouched
by the index for the same null-distinctness reason.

Revision ID: c9e2a5f7d431
Revises: b6d94a2c7e15
Create Date: 2026-08-18
"""
from alembic import op
import sqlalchemy as sa

revision = 'c9e2a5f7d431'
down_revision = 'b6d94a2c7e15'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('rooms', sa.Column('subject_type', sa.String(),
                                     nullable=True))
    op.add_column('rooms', sa.Column('subject_id', sa.String(),
                                     nullable=True))
    op.create_index('uq_rooms_user_type_subject', 'rooms',
                    ['user_uuid', 'room_type', 'subject_id'], unique=True)


def downgrade() -> None:
    op.drop_index('uq_rooms_user_type_subject', table_name='rooms')
    op.drop_column('rooms', 'subject_id')
    op.drop_column('rooms', 'subject_type')
