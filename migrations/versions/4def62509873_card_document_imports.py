"""Keep manual document provenance separate from collection success."""
from alembic import op
from app.models.card_document_import import CardDocumentImport

revision = '4def62509873'
down_revision = '3cdf51498762'
branch_labels = None
depends_on = None


def upgrade():
    CardDocumentImport.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    CardDocumentImport.__table__.drop(op.get_bind(), checkfirst=True)
