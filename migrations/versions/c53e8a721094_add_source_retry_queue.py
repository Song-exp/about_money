"""Persist source retry delays and review states."""
from alembic import op
import sqlalchemy as sa
from app.core.types import PKType

revision = "c53e8a721094"
down_revision = "b42fa7d19208"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("source_retry_jobs",
        sa.Column("id", PKType, primary_key=True, autoincrement=True),
        sa.Column("content_key", sa.String(64), unique=True, nullable=False),
        sa.Column("source_platform", sa.String(100), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("ix_source_retry_jobs_source_platform", "source_retry_jobs", ["source_platform"])


def downgrade():
    op.drop_table("source_retry_jobs")
