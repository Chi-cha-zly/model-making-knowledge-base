"""Fix the existing 1024-dimensional space and index cosine nearest neighbours."""

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision = '0011_vector_search'
down_revision = '0010_image_analysis'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    invalid = bind.scalar(
        sa.text("""SELECT count(*) FROM embeddings WHERE
        (dimensions IS NOT NULL AND dimensions <> 1024) OR
        (vector IS NOT NULL AND (vector_dims(vector) <> 1024 OR
          dimensions IS NULL OR vector_norm(vector) = 0)) OR
        (status = 'ready' AND (vector IS NULL OR model IS NULL))""")
    )
    if invalid:
        raise ValueError(
            f'{invalid} embeddings violate the 1024-dimensional contract; no values were changed'
        )
    op.alter_column(
        'embeddings',
        'vector',
        existing_type=Vector(),
        type_=Vector(1024),
        postgresql_using='vector::vector(1024)',
    )
    op.create_check_constraint(
        'ck_embedding_fixed_dimensions', 'embeddings', 'dimensions IS NULL OR dimensions = 1024'
    )
    op.create_index(
        'ix_embeddings_vector_cosine',
        'embeddings',
        ['vector'],
        postgresql_using='hnsw',
        postgresql_ops={'vector': 'vector_cosine_ops'},
    )


def downgrade():
    op.drop_index('ix_embeddings_vector_cosine', table_name='embeddings')
    op.drop_constraint('ck_embedding_fixed_dimensions', 'embeddings', type_='check')
    op.alter_column(
        'embeddings',
        'vector',
        existing_type=Vector(1024),
        type_=Vector(),
        postgresql_using='vector::vector',
    )
