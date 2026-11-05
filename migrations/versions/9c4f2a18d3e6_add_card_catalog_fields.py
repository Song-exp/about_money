"""카드 선택 화면에 필요한 종류와 공식 이미지 주소."""
from alembic import op
import sqlalchemy as sa

revision = '9c4f2a18d3e6'
down_revision = '8b3e1f07c2d4'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('cards', sa.Column('card_type', sa.String(20)))
    op.add_column('cards', sa.Column('image_url', sa.Text()))


def downgrade():
    op.drop_column('cards', 'image_url')
    op.drop_column('cards', 'card_type')
