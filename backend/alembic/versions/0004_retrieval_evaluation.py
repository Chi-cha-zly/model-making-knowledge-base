"""Persist evaluation datasets and immutable retrieval reports.

Revision ID: 0004_retrieval_evaluation
Revises: 0003_agent_chat
"""

from alembic import op
import sqlalchemy as sa

revision = '0004_retrieval_evaluation'
down_revision = '0003_agent_chat'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'evaluation_datasets',
        sa.Column('id', sa.String(80), primary_key=True),
        sa.Column('payload', sa.JSON, nullable=False),
    )
    op.create_table(
        'evaluation_runs',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('payload', sa.JSON, nullable=False),
    )


def downgrade():
    op.drop_table('evaluation_runs')
    op.drop_table('evaluation_datasets')
