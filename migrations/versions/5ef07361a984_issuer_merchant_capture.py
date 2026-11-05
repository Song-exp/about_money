"""Add query-scoped issuer merchant evidence."""
from alembic import op
from app.models.issuer_merchant_capture import IssuerMerchantCapture

revision = '5ef07361a984'
down_revision = '4def62509873'
branch_labels = None
depends_on = None


def upgrade():
    IssuerMerchantCapture.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    IssuerMerchantCapture.__table__.drop(op.get_bind(), checkfirst=True)
