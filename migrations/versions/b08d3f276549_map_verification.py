"""Add isolated evidence/location/worker lease tables; preserve legacy records."""
from alembic import op
from app.models.map_verification import MapEvidence, MapLocation, MapJobLease, MapReviewEvent

revision = 'b08d3f276549'
down_revision = 'a97c2e165438'
branch_labels = None
depends_on = None


def upgrade():
    for model in (MapEvidence, MapLocation, MapJobLease, MapReviewEvent):
        model.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    for model in (MapReviewEvent, MapJobLease, MapLocation, MapEvidence):
        model.__table__.drop(op.get_bind(), checkfirst=True)
