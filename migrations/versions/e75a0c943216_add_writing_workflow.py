"""Persist confirmed facts, drafts and the saved application link."""
from alembic import op
import sqlalchemy as sa
from app.core.types import GUIDType, JSONType

revision = "e75a0c943216"
down_revision = "d64f9b832105"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("writing_sessions") as batch:
        batch.add_column(sa.Column("workflow", JSONType, nullable=False, server_default=sa.text("'{}'")))
        batch.add_column(sa.Column("application_id", GUIDType, nullable=True))
        batch.create_foreign_key("fk_writing_application", "user_applications", ["application_id"], ["id"], ondelete="SET NULL")


def downgrade():
    with op.batch_alter_table("writing_sessions") as batch:
        batch.drop_constraint("fk_writing_application", type_="foreignkey")
        batch.drop_column("application_id")
        batch.drop_column("workflow")
