"""Diagnostic tool-call steps for agent chat runs."""

from alembic import op
import sqlalchemy as sa

revision = '0012_agent_traces'
down_revision = '0011_vector_search'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'agent_traces',
        sa.Column(
            'run_id', sa.Integer, sa.ForeignKey('runs.id', ondelete='CASCADE'), primary_key=True
        ),
        sa.Column('steps', sa.JSON, nullable=False),
    )


def downgrade():
    op.drop_table('agent_traces')
