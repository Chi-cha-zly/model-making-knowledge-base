"""Versioned input identity and structured image interpretation."""
from alembic import op
import sqlalchemy as sa

revision = '0010_image_analysis'
down_revision = '0009_background_jobs'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('image_analyses',
        sa.Column('attachment_id', sa.Integer(), sa.ForeignKey('attachments.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('image_hash', sa.String(64), nullable=False),
        sa.Column('model', sa.String(200), nullable=False),
        sa.Column('ocr_text', sa.Text(), nullable=False),
        sa.Column('description', sa.JSON(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False))


def downgrade():
    op.drop_table('image_analyses')
