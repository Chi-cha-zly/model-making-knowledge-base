"""Authorized hybrid search with synthetic embeddings and no outbound network."""

import asyncio

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from pg_test_support import test_database_url
from app.errors import ProviderError
from app.ingestion_models import Document, Embedding, Chunk
from app.models import Source, Technique
from test_vector_contract import vector


class QueryProvider:
    embedding_configured = True
    embedding_model = 'text-embedding-v4'

    def __init__(self, action=None):
        self.action = action
        self.calls = []

    async def embed_query(self, query):
        self.calls.append(query)
        if self.action:
            self.action()
        return vector()


def seed():
    engine = create_engine(test_database_url())
    from app.services.ingestion import IngestionService

    with Session(engine) as db:
        source = Source(title='本机证据')
        rows = [
            Technique(title='金属亮面', summary='明亮反光区域逐层收窄', sources=[source]),
            Technique(title='肩甲处理', summary='肩甲边缘使用漆号', sources=[source]),
        ]
        db.add_all(rows)
        db.flush()
        for row in rows:
            IngestionService(db).reindex(row)
        db.flush()
        for embedding, chunk in db.execute(select(Embedding, Chunk).join(Chunk)):
            embedding.vector = (
                vector() if chunk.payload['id'] == rows[0].id else [0.0, 1.0] + [0.0] * 1022
            )
            embedding.status = 'ready'
            embedding.model = 'text-embedding-v4'
            embedding.dimensions = 1024
        db.commit()
        ids = [row.id for row in rows]
    return engine, ids


def service(engine, provider, **kwargs):
    from app.services.retrieval import RetrievalService

    return RetrievalService(engine, provider_getter=lambda: provider, **kwargs)


def test_semantic_only_hit_survives_fusion_and_local_rerank():
    engine, ids = seed()
    provider = QueryProvider()
    result = asyncio.run(service(engine, provider).search('表面反射光亮', strategy='hybrid'))
    assert result.effective_strategy == 'hybrid'
    assert result.hits[0]['id'] == ids[0]
    assert any('向量' in reason for reason in result.hits[0]['reasons'])
    assert result.hits[0]['source_ids']
    assert provider.calls == ['表面反射光亮']


def test_lexical_default_never_calls_provider():
    engine, ids = seed()
    provider = QueryProvider()
    result = asyncio.run(service(engine, provider).search('肩甲'))
    assert result.effective_strategy == 'bm25'
    assert result.hits[0]['id'] == ids[1]
    assert provider.calls == []


def test_withdrawal_during_query_and_final_revalidation_drop_evidence():
    engine, ids = seed()

    def withdraw():
        with Session(engine) as db:
            db.get(Technique, ids[0]).status = 'withdrawn'
            db.commit()

    provider = QueryProvider(withdraw)
    retrieval = service(engine, provider)
    result = asyncio.run(retrieval.search('肩甲', strategy='hybrid'))
    assert ids[0] not in [hit['id'] for hit in result.hits]
    with Session(engine) as db:
        for source in db.scalars(select(Source)):
            source.status = 'withdrawn'
        db.commit()
    assert retrieval.revalidate(result).hits == []


@pytest.mark.parametrize('failure', ['provider', 'dimensions', 'timeout'])
def test_provider_failure_is_visible_lexical_fallback(failure, caplog):
    engine, ids = seed()

    class Failed(QueryProvider):
        async def embed_query(self, query):
            if failure == 'dimensions':
                return [1.0]
            if failure == 'timeout':
                raise TimeoutError('secret query payload')
            raise ProviderError('secret query payload')

    result = asyncio.run(service(engine, Failed()).search('肩甲', strategy='hybrid'))
    assert result.effective_strategy == 'bm25'
    assert result.diagnostics['fallback_reason']
    assert result.hits[0]['id'] == ids[1]
    assert 'secret query payload' not in caplog.text


def test_cancel_is_not_converted_to_success():
    engine, _ = seed()

    class Cancelled(QueryProvider):
        async def embed_query(self, query):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service(engine, Cancelled()).search('肩甲', strategy='hybrid'))


def test_query_is_bounded_and_provider_swap_is_observed():
    engine, _ = seed()
    from app.services.retrieval import RetrievalService

    first, second = QueryProvider(), QueryProvider()
    current = [first]
    retrieval = RetrievalService(engine, provider_getter=lambda: current[0])
    query = '甲' * 2000 + '尾' * 1000
    result = asyncio.run(retrieval.search('肩甲', strategy='hybrid', semantic_query=query))
    assert first.calls == [query[:1500] + query[-500:]]
    assert result.diagnostics['query_truncated'] is True
    current[0] = second
    asyncio.run(retrieval.search('肩甲', strategy='hybrid'))
    assert second.calls == ['肩甲']


def test_final_revalidation_drops_changed_active_version():
    engine, _ = seed()
    retrieval = service(engine, QueryProvider())
    result = asyncio.run(retrieval.search('肩甲', strategy='hybrid'))
    with Session(engine) as db:
        for document in db.scalars(select(Document)):
            document.active_version += 1
        db.commit()
    assert retrieval.revalidate(result).hits == []


def test_title_only_ready_embedding_is_semantically_searchable():
    from app.services.ingestion import IngestionService
    from app.embedding_jobs import embed_pending

    engine = create_engine(test_database_url())
    with Session(engine) as db:
        row = Technique(title='靛蓝罩染', sources=[Source(title='出处')])
        db.add(row)
        IngestionService(db).reindex(row)
        db.commit()

    class Encoder(QueryProvider):
        async def embed(self, texts):
            assert texts == ['靛蓝罩染']
            return [vector()]

    provider = Encoder()
    assert (
        asyncio.run(embed_pending(engine, provider, apply=True))['documents'][0]['status']
        == 'ready'
    )
    result = asyncio.run(service(engine, provider).search('透明冷色薄层', strategy='hybrid'))
    assert result.hits and result.hits[0]['title'] == '靛蓝罩染'


def test_repeated_vector_chunks_do_not_receive_extra_rrf_votes():
    from app.ingestion import digest

    engine, ids = seed()
    with Session(engine) as db:
        original = db.scalar(select(Chunk).where(Chunk.payload['id'].as_integer() == ids[0]))
        for index in range(10):
            chunk = Chunk(
                document_id=original.document_id,
                version=original.version,
                ordinal=100 + index,
                content='另外的语义文本',
                payload=original.payload,
            )
            db.add(chunk)
            db.flush()
            db.add(
                Embedding(
                    chunk_id=chunk.id,
                    content_hash=digest(chunk.content),
                    vector=vector(),
                    status='ready',
                    model='text-embedding-v4',
                    dimensions=1024,
                )
            )
        db.commit()
    result = asyncio.run(service(engine, QueryProvider()).search('未有词元', strategy='hybrid'))
    assert result.hits[0]['score'] == pytest.approx(1 / 61, abs=1e-8)


@pytest.mark.parametrize('condition', ['foreign', 'no_ready', 'wrong_model'])
def test_unusable_corpus_is_not_sent_for_embedding(condition):
    engine, _ = seed()
    with Session(engine) as db:
        if condition == 'foreign':
            for row in db.scalars(select(Technique)):
                row.owner_id = 'someone-else'
        else:
            for row in db.scalars(select(Embedding)):
                if condition == 'no_ready':
                    row.status = 'pending'
                else:
                    row.model = 'other-model'
        db.commit()
    provider = QueryProvider()
    result = asyncio.run(service(engine, provider).search('肩甲', strategy='hybrid'))
    assert result.diagnostics['fallback_reason'] == 'no_ready_vectors'
    assert provider.calls == []


def test_database_error_is_not_reported_as_lexical_success(monkeypatch):
    engine, _ = seed()
    from app.repositories import retrieval as repository

    def broken(*args, **kwargs):
        raise RuntimeError('database unavailable')

    monkeypatch.setattr(repository, 'lexical_search', broken)
    with pytest.raises(RuntimeError, match='database unavailable'):
        asyncio.run(service(engine, QueryProvider()).search('肩甲'))


def test_query_provider_holds_no_database_connection():
    engine, _ = seed()
    provider = QueryProvider(lambda: None)

    async def query(text):
        assert engine.pool.checkedout() == 0
        return vector()

    provider.embed_query = query
    assert asyncio.run(service(engine, provider).search('肩甲', strategy='hybrid')).hits


def test_long_dual_route_hit_keeps_semantic_image_and_lexical_evidence():
    from app.models import Attachment
    from app.services.ingestion import IngestionService

    engine = create_engine(test_database_url())
    with Session(engine) as db:
        image = Attachment(
            filename='a.png',
            content_type='image/png',
            storage_name='a.png',
            sha256='a' * 64,
            size=1,
        )
        db.add(image)
        db.flush()
        row = Technique(
            title='长教程',
            sources=[Source(title='source')],
            attachments=[image],
            content_blocks=[
                {'type': 'paragraph', 'text': '肩甲磨砂准备' + ('不同位置的操作说明' * 180)}
                for _ in range(5)
            ]
            + [{'type': 'image', 'attachment_id': image.id, 'caption': '反光轮廓逐层收窄'}],
        )
        db.add(row)
        IngestionService(db).reindex(row)
        unrelated = Technique(title='另一个语义候选', summary='高反差反射', sources=row.sources)
        db.add(unrelated)
        IngestionService(db).reindex(unrelated)
        for embedding, chunk in db.execute(select(Embedding, Chunk).join(Chunk)):
            embedding.vector = (
                [0.8, 0.6] + [0.0] * 1022
                if chunk.attachment_id
                else vector()
                if chunk.payload['id'] == unrelated.id
                else [0.0, 1.0] + [0.0] * 1022
            )
            embedding.model, embedding.dimensions, embedding.status = (
                'text-embedding-v4',
                1024,
                'ready',
            )
        db.commit()
        image_id = image.id
    retrieval = service(engine, QueryProvider())
    result = asyncio.run(retrieval.search('肩甲磨砂', strategy='hybrid'))
    assert result.hits[0]['attachment_ids'] == [image_id]
    assert '反光轮廓逐层收窄' in result.hits[0]['excerpt']
    assert '肩甲磨砂' in result.hits[0]['excerpt']
    assert len(result.hits[0]['excerpt']) <= 3000
    with Session(engine) as db:
        db.get(Attachment, image_id).sha256 = 'b' * 64
        db.commit()
    assert result.hits[0]['key'] not in [hit['key'] for hit in retrieval.revalidate(result).hits]
    fresh = asyncio.run(retrieval.search('肩甲磨砂', strategy='hybrid'))
    assert fresh.hits[0]['attachment_ids'] == []
    assert '反光轮廓' not in fresh.hits[0]['excerpt']


def test_exact_sku_rule_can_lift_lower_vector_document_without_losing_semantic_hits():
    from app.models import Product
    from app.services.ingestion import IngestionService

    engine, ids = seed()
    with Session(engine) as db:
        product = Product(name='颜料', sku='AK-12345', sources=[db.scalar(select(Source))])
        db.add(product)
        IngestionService(db).reindex(product)
        db.commit()
        product_id = product.id
    result = asyncio.run(service(engine, QueryProvider()).search('AK 12345', strategy='hybrid'))
    assert result.hits[0]['key'] == f'product:{product_id}'
    assert any('编号规范化精确匹配' in reason for reason in result.hits[0]['reasons'])
    assert f'technique:{ids[0]}' in [hit['key'] for hit in result.hits]
