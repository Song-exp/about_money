"""Add immutable decoded supporting-document text."""
from alembic import op
from app.models.card_document_text import CardDocumentText

revision = '3cdf51498762'
down_revision = '2bce40387651'
branch_labels = None
depends_on = None


def upgrade():
    CardDocumentText.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    CardDocumentText.__table__.drop(op.get_bind(), checkfirst=True)
