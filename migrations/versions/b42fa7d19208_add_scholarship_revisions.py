"""Preserve structured scholarship versions."""
from alembic import op
import sqlalchemy as sa
from app.core.types import JSONType, PKType

revision = "b42fa7d19208"
down_revision = "a31f920c4e11"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("scholarship_revisions",
        sa.Column("id", PKType, primary_key=True, autoincrement=True),
        sa.Column("scholarship_id", PKType, sa.ForeignKey("scholarships.id"), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("snapshot", JSONType, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("scholarship_id", "sha256", name="uq_scholarship_revision_hash"),
    )
    op.create_index("ix_scholarship_revisions_scholarship_id", "scholarship_revisions", ["scholarship_id"])


def downgrade():
    op.drop_table("scholarship_revisions")
