"""P0 knowledge catalog and search indexes.

Revision ID: 0001_initial
Revises:
"""

from alembic import op
import sqlalchemy as sa


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table("sources",
                    sa.Column("id", sa.Integer, primary_key=True), sa.Column("title", sa.String(300), nullable=False),
                    sa.Column("url", sa.Text), sa.Column("source_type", sa.String(30), nullable=False),
                    sa.Column("author", sa.String(300)), sa.Column("license_note", sa.Text),
                    sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False), sa.Column("content_hash", sa.String(64)),
                    sa.Column("owner_id", sa.String(64), nullable=False), sa.Column("visibility", sa.String(20), nullable=False),
                    sa.Column("status", sa.String(20), nullable=False))
    op.create_table("techniques",
                    sa.Column("id", sa.Integer, primary_key=True), sa.Column("title", sa.String(300), nullable=False),
                    sa.Column("category", sa.String(100)), sa.Column("summary", sa.Text), sa.Column("steps", sa.JSON, nullable=False),
                    sa.Column("prerequisites", sa.Text), sa.Column("risks", sa.Text), sa.Column("difficulty", sa.String(50)),
                    sa.Column("tags", sa.JSON, nullable=False), sa.Column("owner_id", sa.String(64), nullable=False),
                    sa.Column("visibility", sa.String(20), nullable=False), sa.Column("status", sa.String(20), nullable=False))
    op.create_table("products",
                    sa.Column("id", sa.Integer, primary_key=True), sa.Column("brand", sa.String(200)),
                    sa.Column("name", sa.String(300), nullable=False), sa.Column("sku", sa.String(150)),
                    sa.Column("sku_key", sa.String(150), unique=True), sa.Column("kind", sa.String(100)),
                    sa.Column("properties", sa.JSON, nullable=False), sa.Column("manufacturer_url", sa.Text),
                    sa.Column("owner_id", sa.String(64), nullable=False), sa.Column("visibility", sa.String(20), nullable=False),
                    sa.Column("status", sa.String(20), nullable=False))
    op.create_table("product_aliases",
                    sa.Column("id", sa.Integer, primary_key=True),
                    sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
                    sa.Column("alias", sa.String(300), nullable=False), sa.Column("alias_key", sa.String(500), unique=True, nullable=False))
    op.create_table("technique_sources",
                    sa.Column("technique_id", sa.Integer, sa.ForeignKey("techniques.id", ondelete="CASCADE"), primary_key=True),
                    sa.Column("source_id", sa.Integer, sa.ForeignKey("sources.id", ondelete="RESTRICT"), primary_key=True))
    op.create_table("product_sources",
                    sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id", ondelete="CASCADE"), primary_key=True),
                    sa.Column("source_id", sa.Integer, sa.ForeignKey("sources.id", ondelete="RESTRICT"), primary_key=True))
    op.create_table("attachments",
                    sa.Column("id", sa.Integer, primary_key=True), sa.Column("filename", sa.String(300), nullable=False),
                    sa.Column("content_type", sa.String(40), nullable=False), sa.Column("storage_name", sa.String(64), unique=True, nullable=False),
                    sa.Column("sha256", sa.String(64), nullable=False), sa.Column("size", sa.Integer, nullable=False),
                    sa.Column("owner_id", sa.String(64), nullable=False), sa.Column("visibility", sa.String(20), nullable=False),
                    sa.Column("status", sa.String(20), nullable=False))
    if op.get_bind().dialect.name == "postgresql":
        for table, field in (("techniques", "title"), ("products", "name"), ("products", "sku"), ("product_aliases", "alias")):
            op.execute(f"CREATE INDEX ix_{table}_{field}_trgm ON {table} USING gin ({field} gin_trgm_ops)")


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        for table, field in (("techniques", "title"), ("products", "name"), ("products", "sku"), ("product_aliases", "alias")):
            op.drop_index(f"ix_{table}_{field}_trgm", table_name=table)
    for table in ("attachments", "product_sources", "technique_sources", "product_aliases", "products", "techniques", "sources"):
        op.drop_table(table)
