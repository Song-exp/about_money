"""Encrypted CODEF card connections bound to authenticated users."""
from alembic import op
import sqlalchemy as sa
from app.core.types import GUIDType

revision = 'a97c2e165438'
down_revision = 'f86b1d054327'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('card_connections',
        sa.Column('user_id', GUIDType, sa.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('organization', sa.String(4), primary_key=True),
        sa.Column('environment', sa.String(12), primary_key=True),
        sa.Column('encrypted_connected_id', sa.Text(), nullable=False),
        sa.Column('login_type', sa.String(1), nullable=False),
        sa.Column('consent_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('last_queried_at', sa.DateTime(timezone=True)))


def downgrade():
    op.drop_table('card_connections')
