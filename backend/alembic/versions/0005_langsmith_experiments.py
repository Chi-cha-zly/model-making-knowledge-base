"""Persist bounded LangSmith experiment jobs."""
from alembic import op
import sqlalchemy as sa
revision = '0005_langsmith_experiments'
down_revision = '0004_retrieval_evaluation'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table('experiment_jobs', sa.Column('id',sa.Integer,primary_key=True), sa.Column('request_id',sa.String(100),nullable=False,unique=True), sa.Column('request_hash',sa.String(64),nullable=False), sa.Column('payload',sa.JSON,nullable=False), sa.Column('snapshot',sa.JSON,nullable=False))

def downgrade():
    op.drop_table('experiment_jobs')
