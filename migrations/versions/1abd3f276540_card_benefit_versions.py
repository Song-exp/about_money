"""Preserve extracted card benefit versions and their field evidence."""
from alembic import op
from app.models.card_benefit_version import BENEFIT_VERSION_MODELS

revision = '1abd3f276540'
down_revision = '09ac2e165439'
branch_labels = None
depends_on = None


def upgrade():
    for model in BENEFIT_VERSION_MODELS:
        model.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    for model in reversed(BENEFIT_VERSION_MODELS):
        model.__table__.drop(op.get_bind(), checkfirst=True)
