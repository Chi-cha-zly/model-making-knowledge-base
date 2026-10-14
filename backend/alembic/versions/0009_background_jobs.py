"""Shared background job lifecycle."""
from alembic import op
import sqlalchemy as sa

revision = '0009_background_jobs'
down_revision = '0008_postgres_blocks'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('jobs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('kind', sa.String(40), nullable=False),
        sa.Column('request_id', sa.String(128), nullable=False),
        sa.Column('request_hash', sa.String(64), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('progress', sa.Integer(), nullable=False),
        sa.Column('total', sa.Integer(), nullable=False),
        sa.Column('result', sa.JSON(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('kind', 'request_id'))
    op.create_index('ix_jobs_status', 'jobs', ['status'])


def downgrade():
    op.drop_table('jobs')
