"""add phone authentication (verification by incoming call)

Adds the columns a phone login needs on ``users`` and the table that tracks
verification attempts.

Attempts live in their own table rather than on ``users`` because they are
short-lived, numerous, and exist for numbers that may never become accounts —
rate limiting reads them by phone and by ip.

Every step is inspector-guarded: ``0001`` builds fresh schemas via
``Base.metadata.create_all(checkfirst=True)``, so on a new install these objects
already exist by the time this revision runs.

Revision ID: 0105
Revises: 0104
"""

import sqlalchemy as sa
from alembic import op

revision = '0105'
down_revision = '0104'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    users_columns = {c['name'] for c in inspector.get_columns('users')}

    if 'phone' not in users_columns:
        op.add_column('users', sa.Column('phone', sa.String(length=20), nullable=True))
        op.create_index('ix_users_phone', 'users', ['phone'], unique=True)
    if 'phone_verified' not in users_columns:
        op.add_column(
            'users',
            sa.Column('phone_verified', sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if 'phone_verified_at' not in users_columns:
        op.add_column('users', sa.Column('phone_verified_at', sa.DateTime(timezone=True), nullable=True))

    if 'phone_auth_attempts' not in inspector.get_table_names():
        op.create_table(
            'phone_auth_attempts',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('phone', sa.String(length=20), nullable=False),
            sa.Column('public_id', sa.String(length=64), nullable=False),
            sa.Column('call_id', sa.String(length=128), nullable=False),
            sa.Column('provider', sa.String(length=32), nullable=False),
            sa.Column('dial_number', sa.String(length=20), nullable=True),
            sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('cost', sa.String(length=16), nullable=True),
            sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('ip', sa.String(length=45), nullable=True),
            sa.Column('user_agent', sa.String(length=512), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index('ix_phone_auth_attempts_phone', 'phone_auth_attempts', ['phone'])
        op.create_index('ix_phone_auth_attempts_public_id', 'phone_auth_attempts', ['public_id'], unique=True)
        op.create_index('ix_phone_auth_attempts_call_id', 'phone_auth_attempts', ['call_id'])
        op.create_index('ix_phone_auth_attempts_created_at', 'phone_auth_attempts', ['created_at'])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if 'phone_auth_attempts' in inspector.get_table_names():
        op.drop_table('phone_auth_attempts')

    users_columns = {c['name'] for c in inspector.get_columns('users')}
    # The unique index goes with the column; dropping it separately would fail
    # on backends that tie index lifetime to the column.
    if 'phone_verified_at' in users_columns:
        op.drop_column('users', 'phone_verified_at')
    if 'phone_verified' in users_columns:
        op.drop_column('users', 'phone_verified')
    if 'phone' in users_columns:
        op.drop_column('users', 'phone')
