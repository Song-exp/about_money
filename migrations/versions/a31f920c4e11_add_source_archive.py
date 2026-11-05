"""Preserve public source bytes and per-document fetch attempts."""
from alembic import op
import sqlalchemy as sa
from app.core.types import JSONType, PKType

revision = "a31f920c4e11"
down_revision = "d2aaf04ab078"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("source_documents",
        sa.Column("id", PKType, primary_key=True, autoincrement=True),
        sa.Column("sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table("source_fetch_attempts",
        sa.Column("id", PKType, primary_key=True, autoincrement=True),
        sa.Column("content_key", sa.String(64), nullable=False),
        sa.Column("source_platform", sa.String(100), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("documents", JSONType, nullable=False),
        sa.Column("listing_metadata", JSONType, nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_source_fetch_attempts_content_key", "source_fetch_attempts", ["content_key"])
    op.create_index("ix_source_fetch_attempts_source_platform", "source_fetch_attempts", ["source_platform"])


def downgrade():
    op.drop_table("source_fetch_attempts")
    op.drop_table("source_documents")
