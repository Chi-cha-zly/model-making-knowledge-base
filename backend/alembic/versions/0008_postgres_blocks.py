"""PostgreSQL vector foundation and single-source technique content."""
from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision = '0008_postgres_blocks'
down_revision = '0007_ingestion'
branch_labels = None
depends_on = None


def _text(value):
    if value is None:
        return ''
    if isinstance(value, bool):
        return '是' if value else '否'
    if isinstance(value, dict):
        return '\n'.join(f'{key}：{_text(item)}' for key, item in value.items())
    if isinstance(value, list):
        return '\n'.join(_text(item) for item in value)
    return str(value)


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        raise RuntimeError('PostgreSQL is required; use the explicit legacy import tool')
    op.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public')
    op.execute('CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public')
    table = sa.table('techniques', sa.column('id', sa.Integer), sa.column('steps', sa.JSON), sa.column('content_blocks', sa.JSON))
    for row in bind.execute(sa.select(table)).mappings():
        if not row['content_blocks'] and row['steps']:
            steps = row['steps'] if isinstance(row['steps'], list) else [row['steps']]
            texts = [_text(step) for step in steps]
            if sum(map(len,texts)) > 100000:
                raise ValueError('Legacy content exceeds 100000 characters; split record before migration')
            parts = [value[start:start+20000] for value in texts for start in range(0,max(1,len(value)),20000)]
            if len(parts) > 200:
                joined = '\n'.join(parts)
                if len(joined) > 100000:
                    raise ValueError('Legacy content exceeds 100000 characters; split record before migration')
                parts = [joined[start:start+20000] for start in range(0,len(joined),20000)]
            blocks = [{'type': 'paragraph', 'text': value} for value in parts]
            bind.execute(table.update().where(table.c.id == row['id']).values(content_blocks=blocks))
    op.drop_column('techniques', 'steps')
    op.execute("UPDATE chunks SET payload = (payload::jsonb || jsonb_build_object('attachment_hash', attachments.sha256))::json FROM attachments WHERE chunks.attachment_id = attachments.id")
    op.alter_column('embeddings', 'vector', type_=Vector(), existing_type=sa.JSON,
                    postgresql_using="NULLIF(vector::text, 'null')::vector")
    op.create_check_constraint('ck_embedding_vector_dimensions', 'embeddings',
        'vector IS NULL OR (dimensions IS NOT NULL AND dimensions = vector_dims(vector))')


def downgrade():
    op.drop_constraint('ck_embedding_vector_dimensions', 'embeddings', type_='check')
    op.alter_column('embeddings', 'vector', type_=sa.JSON, existing_type=Vector(), postgresql_using='vector::text::json')
    op.add_column('techniques', sa.Column('steps', sa.JSON, nullable=False, server_default='[]'))
    table = sa.table('techniques', sa.column('id', sa.Integer), sa.column('steps', sa.JSON), sa.column('content_blocks', sa.JSON))
    bind = op.get_bind()
    for row in bind.execute(sa.select(table)).mappings():
        steps = [b.get('text', b.get('caption', '')) for b in row['content_blocks'] or []]
        bind.execute(table.update().where(table.c.id == row['id']).values(steps=steps))
