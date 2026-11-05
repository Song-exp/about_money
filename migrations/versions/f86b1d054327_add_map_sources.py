"""Track public map sources and validated promotion feeds."""
from alembic import op
import sqlalchemy as sa
from app.core.types import JSONType

revision = 'f86b1d054327'
down_revision = 'e75a0c943216'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('map_source_states',
        sa.Column('source_key', sa.String(100), primary_key=True),
        sa.Column('source_url', sa.Text(), nullable=False),
        sa.Column('source_kind', sa.String(40), nullable=False),
        sa.Column('status', sa.String(30), nullable=False),
        sa.Column('last_attempt_at', sa.DateTime(timezone=True)),
        sa.Column('last_success_at', sa.DateTime(timezone=True)),
        sa.Column('next_retry_at', sa.DateTime(timezone=True)),
        sa.Column('failures', sa.Integer(), nullable=False),
        sa.Column('error', sa.String(150)),
        sa.Column('etag', sa.Text()), sa.Column('last_modified', sa.Text()),
        sa.Column('content_hash', sa.String(64)),
        sa.Column('records', JSONType, nullable=False))


def downgrade():
    op.drop_table('map_source_states')
