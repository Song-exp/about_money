"""CODEF 연동을 내리면서 카드 연결 테이블을 지운다."""
from alembic import op
import sqlalchemy as sa
from app.core.types import GUIDType

revision = '8b3e1f07c2d4'
down_revision = '60a18472ba95'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table('card_connections')


def downgrade():
    op.create_table('card_connections',
        sa.Column('user_id', GUIDType, sa.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('organization', sa.String(4), primary_key=True),
        sa.Column('environment', sa.String(12), primary_key=True),
        sa.Column('encrypted_connected_id', sa.Text(), nullable=False),
        sa.Column('login_type', sa.String(1), nullable=False),
        sa.Column('consent_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('last_queried_at', sa.DateTime(timezone=True)))
