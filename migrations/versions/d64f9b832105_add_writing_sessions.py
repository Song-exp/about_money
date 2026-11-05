"""Persist scholarship-specific writing chats and pinned form versions."""
from alembic import op
import sqlalchemy as sa
from app.core.types import GUIDType, JSONType

revision = "d64f9b832105"
down_revision = "c53e8a721094"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("writing_sessions",
        sa.Column("id", GUIDType, primary_key=True),
        sa.Column("user_id", GUIDType, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scholarship_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), sa.ForeignKey("scholarships.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("revision_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), sa.ForeignKey("scholarship_revisions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("form_fingerprint", sa.String(64), nullable=False),
        sa.Column("profile", JSONType, nullable=False), sa.Column("fields", JSONType, nullable=False),
        sa.Column("messages", JSONType, nullable=False), sa.Column("answers", JSONType, nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "scholarship_id", "form_fingerprint", name="uq_writing_user_scholarship_form"))
    op.create_index("ix_writing_sessions_user_id", "writing_sessions", ["user_id"])
    op.create_index("ix_writing_sessions_scholarship_id", "writing_sessions", ["scholarship_id"])


def downgrade():
    op.drop_table("writing_sessions")
