"""Persist account-owned information chat and search state."""
from alembic import op
from app.models.information_session import InformationSession

revision = '60a18472ba95'
down_revision = '5ef07361a984'
branch_labels = None
depends_on = None


def upgrade():
    InformationSession.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    InformationSession.__table__.drop(op.get_bind(), checkfirst=True)
