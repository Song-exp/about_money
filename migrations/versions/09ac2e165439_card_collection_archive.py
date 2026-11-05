"""Add card issuer discovery and immutable source archive, preserving existing cards."""
from alembic import op
from app.models.card_collection import COLLECTION_MODELS

revision = '09ac2e165439'
down_revision = 'b08d3f276549'
branch_labels = None
depends_on = None


def upgrade():
    for model in COLLECTION_MODELS:
        model.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    for model in reversed(COLLECTION_MODELS):
        model.__table__.drop(op.get_bind(), checkfirst=True)
