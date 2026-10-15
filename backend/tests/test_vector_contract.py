"""Fixed-dimensional production embeddings; synthetic values never leave tests."""

import asyncio
import json

import httpx
import pytest

from app.errors import ProviderError
from app.llm import LangChainProvider, validate_vectors


def vector(first=1.0):
    return [first] + [0.0] * 1023


@pytest.mark.parametrize(
    'value', [[1.0] * 1023, [1.0] * 1025, [0.0] * 1024, [1e100] * 1024, [True] * 1024]
)
def test_fixed_contract_rejects_unusable_vectors(value):
    with pytest.raises(ProviderError):
        validate_vectors([value], 1)


def test_fixed_contract_accepts_real_1024_without_normalization():
    value = vector(7.0)
    assert validate_vectors([value], 1) == 1024
    assert value[0] == 7.0


def test_query_embedding_explicit_dimension_and_ordered_sdk_response():
    def handler(request):
        body = json.loads(request.content)
        assert body['dimensions'] == 1024
        assert body['input'] == ['肩甲高光']
        assert 'text_type' not in body
        return httpx.Response(200, json={'data': [{'index': 0, 'embedding': vector()}]})

    provider = LangChainProvider(
        settings={
            'EMBEDDING_MODEL': 'text-embedding-v4',
            'EMBEDDING_BASE_URL': 'https://embedding.invalid/v1',
            'EMBEDDING_API_KEY': 'fake',
        },
        transport=httpx.MockTransport(handler),
    )
    assert asyncio.run(provider.embed_query('肩甲高光')) == vector()


def test_embedding_model_declares_fixed_dimension_and_cosine_index():
    from app.ingestion_models import Embedding

    assert Embedding.__table__.c.vector.type.dim == 1024
    indexes = list(Embedding.__table__.indexes)
    assert any(index.dialect_options['postgresql']['using'] == 'hnsw' for index in indexes)


def test_fixed_dimension_migration_preserves_history_and_rejects_invalid_rows():
    from pathlib import Path
    from sqlalchemy import create_engine, text
    from alembic import command
    from alembic.config import Config
    from pg_test_support import test_database_url

    engine = create_engine(test_database_url())
    config = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    config.set_main_option('script_location', str(Path(__file__).resolve().parents[1] / 'alembic'))
    with engine.begin() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, '0010_image_analysis')
        connection.execute(
            text(
                "INSERT INTO documents(id,record_key,kind,record_id,active_version,fingerprint,status) VALUES(1,'technique:1','technique',1,1,'','ready')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO chunks(id,document_id,version,ordinal,kind,content,payload) VALUES(1,1,1,0,'text','text','{}')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO embeddings(chunk_id,status,model,dimensions,vector,content_hash) VALUES(1,'ready','text-embedding-v4',1024,CAST(:value AS vector),'hash')"
            ),
            {'value': json.dumps(vector(2))},
        )
        before = connection.scalar(text('SELECT vector::text FROM embeddings'))
        command.upgrade(config, 'head')
        assert connection.scalar(text('SELECT vector::text FROM embeddings')) == before
        assert (
            connection.scalar(
                text(
                    "SELECT format_type(atttypid,atttypmod) FROM pg_attribute WHERE attrelid='embeddings'::regclass AND attname='vector'"
                )
            )
            == 'vector(1024)'
        )
        command.downgrade(config, '0010_image_analysis')
        assert connection.scalar(text('SELECT vector::text FROM embeddings')) == before
        connection.execute(text("UPDATE embeddings SET vector='[1,2]', dimensions=2"))
        with pytest.raises(ValueError, match='no values were changed'):
            command.upgrade(config, 'head')
        assert connection.scalar(text('SELECT vector_dims(vector) FROM embeddings')) == 2
    engine.dispose()
