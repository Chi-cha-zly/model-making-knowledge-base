"""Persist ordered technique article blocks."""
from alembic import op
import sqlalchemy as sa

revision = '0006_technique_content'
down_revision = '0005_langsmith_experiments'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('techniques', sa.Column('content_blocks', sa.JSON(), nullable=False, server_default=sa.text("'[]'")))


def downgrade():
    op.drop_column('techniques', 'content_blocks')
