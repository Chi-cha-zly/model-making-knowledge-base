"""Versioned ingestion and indexed lexical candidates.

Existing records are backfilled once at application startup (missing documents
only), or by the explicit backfill endpoint. This schema migration does not call
models or read attachment files.
"""
from alembic import op
import sqlalchemy as sa

revision = '0007_ingestion'
down_revision = '0006_technique_content'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('documents',
                    sa.Column('id', sa.Integer, primary_key=True),
                    sa.Column('record_key', sa.String(100), nullable=False, unique=True),
                    sa.Column('kind', sa.String(20), nullable=False),
                    sa.Column('record_id', sa.Integer, nullable=False),
                    sa.Column('active_version', sa.Integer, nullable=False),
                    sa.Column('fingerprint', sa.String(64), nullable=False),
                    sa.Column('status', sa.String(20), nullable=False))
    op.create_table('document_sources',
                    sa.Column('document_id', sa.Integer, sa.ForeignKey('documents.id', ondelete='CASCADE'), primary_key=True),
                    sa.Column('version', sa.Integer, primary_key=True),
                    sa.Column('source_id', sa.Integer, sa.ForeignKey('sources.id', ondelete='RESTRICT'), primary_key=True))
    op.create_table('chunks',
                    sa.Column('id', sa.Integer, primary_key=True),
                    sa.Column('document_id', sa.Integer, sa.ForeignKey('documents.id', ondelete='CASCADE'), nullable=False),
                    sa.Column('version', sa.Integer, nullable=False),
                    sa.Column('ordinal', sa.Integer, nullable=False),
                    sa.Column('kind', sa.String(20), nullable=False),
                    sa.Column('content', sa.Text, nullable=False),
                    sa.Column('attachment_id', sa.Integer, sa.ForeignKey('attachments.id', ondelete='CASCADE')),
                    sa.Column('payload', sa.JSON, nullable=False),
                    sa.UniqueConstraint('document_id', 'version', 'ordinal'))
    op.create_index('ix_chunks_document_id', 'chunks', ['document_id'])
    op.create_table('embeddings',
                    sa.Column('id', sa.Integer, primary_key=True),
                    sa.Column('chunk_id', sa.Integer, sa.ForeignKey('chunks.id', ondelete='CASCADE'), nullable=False, unique=True),
                    sa.Column('status', sa.String(20), nullable=False),
                    sa.Column('model', sa.String(200)), sa.Column('dimensions', sa.Integer),
                    sa.Column('vector', sa.JSON), sa.Column('content_hash', sa.String(64), nullable=False))
    op.create_table('ingestion_jobs',
                    sa.Column('id', sa.Integer, primary_key=True),
                    sa.Column('document_id', sa.Integer, sa.ForeignKey('documents.id', ondelete='CASCADE'), nullable=False),
                    sa.Column('version', sa.Integer, nullable=False),
                    sa.Column('status', sa.String(20), nullable=False),
                    sa.Column('progress', sa.Integer, nullable=False), sa.Column('error', sa.Text))
    op.create_index('ix_ingestion_jobs_document_id', 'ingestion_jobs', ['document_id'])
    op.create_table('chunk_postings',
                    sa.Column('chunk_id', sa.Integer, sa.ForeignKey('chunks.id', ondelete='CASCADE'), primary_key=True),
                    sa.Column('term', sa.String(200), primary_key=True))
    op.create_index('ix_chunk_postings_term_chunk', 'chunk_postings', ['term', 'chunk_id'])


def downgrade():
    for table in ('chunk_postings', 'embeddings', 'ingestion_jobs', 'chunks', 'document_sources', 'documents'):
        op.drop_table(table)
