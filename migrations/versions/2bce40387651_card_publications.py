"""Add current reviewed program publication pointer."""
from alembic import op
from app.models.card_publication import CardProgramPublication

revision = '2bce40387651'
down_revision = '1abd3f276540'
branch_labels = None
depends_on = None


def upgrade():
    CardProgramPublication.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    CardProgramPublication.__table__.drop(op.get_bind(), checkfirst=True)
