"""Bounded NMM authorization and call accounting independent of graph state."""

from alembic import op
import sqlalchemy as sa

revision = '0015_nmm_accounting'
down_revision = '0014_nmm_checkpoints'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('nmm_projects', sa.Column('meta', sa.JSON(), nullable=False, server_default='{}'))


def downgrade():
    op.drop_column('nmm_projects', 'meta')
