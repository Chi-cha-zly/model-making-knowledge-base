"""Link images to techniques and products.

Revision ID: 0002_attachment_links
Revises: 0001_initial
"""

from alembic import op
import sqlalchemy as sa


revision = "0002_attachment_links"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("technique_attachments",
                    sa.Column("technique_id", sa.Integer, sa.ForeignKey("techniques.id", ondelete="CASCADE"), primary_key=True),
                    sa.Column("attachment_id", sa.Integer, sa.ForeignKey("attachments.id", ondelete="RESTRICT"), primary_key=True))
    op.create_table("product_attachments",
                    sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id", ondelete="CASCADE"), primary_key=True),
                    sa.Column("attachment_id", sa.Integer, sa.ForeignKey("attachments.id", ondelete="RESTRICT"), primary_key=True))


def downgrade():
    op.drop_table("product_attachments")
    op.drop_table("technique_attachments")
