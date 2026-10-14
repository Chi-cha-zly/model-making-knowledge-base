from pg_test_support import test_database_url
"""Real LangChain/SDK calls over MockTransport; database effects use temporary SQLite."""
import asyncio
import json
import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.errors import ProviderError
from app.llm import LangChainProvider
from app.models import Base, Source, Technique, technique_sources
from app.ingestion_models import Document, Chunk, Embedding, DocumentSource
from app.ingestion import digest


def configuration():
    return {'EMBEDDING_BASE_URL': 'https://embed.invalid/v1',
            'EMBEDDING_API_KEY': 'embedding-test-key', 'EMBEDDING_MODEL': 'encoder-v1'}


def response(vectors, indices=None):
    return httpx.Response(200, text=json.dumps({'object': 'list', 'model': 'encoder-v1',
        'data': [{'object': 'embedding', 'index': index, 'embedding': vector}
                 for index, vector in zip(indices if indices is not None else range(len(vectors)), vectors)],
        'usage': {'prompt_tokens': 3, 'total_tokens': 3}}))


def provider(handler, configured=True):
    return LangChainProvider(settings=configuration() if configured else {}, transport=httpx.MockTransport(handler))


def seed(tmp_path, count=3):
    engine = create_engine(test_database_url(f'{tmp_path / "embed.db"}'))
    with engine.begin() as connection:
        connection.execute(Source.__table__.insert().values(id=1, title='source'))
        connection.execute(Technique.__table__.insert().values(id=1, title='technique'))
        connection.execute(technique_sources.insert().values(technique_id=1, source_id=1))
    with Session(engine) as db:
        document = Document(record_key='technique:1', kind='technique', record_id=1, active_version=2, status='ready')
        db.add(document)
        db.flush()
        for version, texts in [(1, ['old']), (2, [f'text {i}' for i in range(count)])]:
            db.add(DocumentSource(document_id=document.id, version=version, source_id=1))
            for ordinal, text in enumerate(texts):
                chunk = Chunk(document_id=document.id, version=version, ordinal=ordinal, content=text, payload={})
                db.add(chunk)
                db.flush()
                db.add(Embedding(chunk_id=chunk.id, content_hash=digest(text)))
        db.commit()
    return engine


def rows(engine):
    with Session(engine) as db:
        return [(c.version, c.content, e.status, e.model, e.dimensions, e.vector, e.content_hash)
                for e, c in db.execute(select(Embedding, Chunk).join(Chunk).order_by(Chunk.id))]


def test_embedding_configuration_is_independent_and_missing_never_requests():
    calls = []
    p = LangChainProvider(settings={'AI_BASE_URL': 'https://chat.invalid/v1', 'AI_API_KEY': 'chat-key', 'AI_MODEL': 'chat'},
                          transport=httpx.MockTransport(lambda request: calls.append(request)))
    assert hasattr(p, 'embed'), 'unified provider must expose explicit embedding capability'
    assert not p.embedding_configured
    with pytest.raises(ProviderError, match='EMBEDDING'):
        asyncio.run(p.embed(['text']))
    assert calls == []


def test_real_sdk_embedding_preserves_input_order_and_uses_separate_endpoint(monkeypatch):
    from langsmith import get_tracing_context
    monkeypatch.setenv('LANGSMITH_TRACING', 'true')
    calls = []
    def handler(request):
        calls.append(request)
        assert get_tracing_context()['enabled'] is False
        return response([[2.0, 0.0], [1.0, 0.5]], [1, 0])
    p = provider(handler)
    assert asyncio.run(p.embed(['first', 'second'])) == [[1.0, 0.5], [2.0, 0.0]]
    assert str(calls[0].url) == 'https://embed.invalid/v1/embeddings'
    assert calls[0].headers['authorization'] == 'Bearer embedding-test-key'
    assert json.loads(calls[0].content) == {'input': ['first', 'second'], 'model': 'encoder-v1', 'encoding_format': 'float'}


@pytest.mark.parametrize('vectors,indices', [([], []), ([[1]], [0]), ([[1], [2, 3]], [0, 1]),
    ([[float('nan')], [2]], [0, 1]), ([[float('inf')], [2]], [0, 1]),
    ([[True], [2]], [0, 1]), ([[1], [2]], [0, 0]), ([[1], [2]], [1, 2]), ([[], []], [0, 1])])
def test_invalid_embedding_result_fails_safely(vectors, indices):
    with pytest.raises(ProviderError):
        asyncio.run(provider(lambda _: response(vectors, indices)).embed(['first', 'second']))


def test_embedding_failure_is_redacted_and_never_automatically_retried():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(500, json={'error': {'message': 'private-secret-body'}})
    with pytest.raises(ProviderError) as error:
        asyncio.run(provider(handler).embed(['text']))
    assert len(calls) == 1 and 'private-secret' not in str(error.value)


def test_dry_run_and_unconfigured_leave_pending_and_do_not_request(tmp_path):
    from app.embedding_jobs import embed_pending
    engine = seed(tmp_path)
    calls = []
    p = provider(lambda r: calls.append(r))
    before = rows(engine)
    plan = asyncio.run(embed_pending(engine, p))
    assert plan['status'] == 'dry_run' and plan['documents'][0]['chunks'] == 3
    missing = asyncio.run(embed_pending(engine, provider(lambda r: calls.append(r), False), apply=True))
    assert missing['status'] == 'unconfigured'
    assert rows(engine) == before and calls == []


def test_batches_atomically_write_only_active_version_and_are_idempotent(tmp_path):
    from app.embedding_jobs import embed_pending
    engine = seed(tmp_path)
    calls = []
    def handler(request):
        texts = json.loads(request.content)['input']
        calls.append(texts)
        assert all(row[2] == 'pending' for row in rows(engine))
        return response([[float(text[-1]), .5] for text in texts])
    p = provider(handler)
    result = asyncio.run(embed_pending(engine, p, apply=True, batch_size=2))
    assert result['status'] == 'completed'
    assert calls == [['text 0', 'text 1'], ['text 2']]
    state = rows(engine)
    assert state[0][2] == 'pending'
    assert all(row[2:5] == ('ready', 'encoder-v1', 2) for row in state[1:])
    assert [row[5] for row in state[1:]] == [[0., .5], [1., .5], [2., .5]]
    assert asyncio.run(embed_pending(engine, p, apply=True))['documents'] == []
    assert len(calls) == 2


def test_failed_batch_is_atomic_retry_preserves_existing_ready(tmp_path):
    from app.embedding_jobs import embed_pending
    engine = seed(tmp_path)
    with Session(engine) as db:
        row = db.scalar(select(Embedding).join(Chunk).where(Chunk.version == 2, Chunk.ordinal == 0))
        row.status, row.model, row.dimensions, row.vector = 'ready', 'encoder-v1', 2, [9., 9.]
        db.commit()
    calls = []
    def fail_second(request):
        calls.append(request)
        return response([[1., 2.]]) if len(calls) == 1 else httpx.Response(503, text='secret-body')
    result = asyncio.run(embed_pending(engine, provider(fail_second), apply=True, batch_size=1))
    assert result['status'] == 'partial_failure'
    assert rows(engine)[1][2:6] == ('ready', 'encoder-v1', 2, [9., 9.])
    assert all(row[2] == 'failed' and row[5] is None for row in rows(engine)[2:])
    assert 'secret' not in json.dumps(result)
    retry = asyncio.run(embed_pending(engine, provider(lambda _: response([[1., 2.], [3., 4.]])), apply=True))
    assert retry['status'] == 'completed'
    assert rows(engine)[1][5] == [9., 9.]
    assert all(row[2] == 'ready' for row in rows(engine)[1:])


def test_version_change_during_request_cannot_publish_stale_vectors(tmp_path):
    from app.embedding_jobs import embed_pending
    engine = seed(tmp_path, count=1)
    def handler(_):
        with Session(engine) as db:
            db.scalar(select(Document)).active_version = 3
            db.commit()
        return response([[1., 2.]])
    result = asyncio.run(embed_pending(engine, provider(handler), apply=True))
    assert result['documents'][0]['status'] == 'stale'
    assert all(row[2] == 'pending' and row[5] is None for row in rows(engine))


def test_dimension_changes_between_batches_are_atomic(tmp_path):
    from app.embedding_jobs import embed_pending
    engine = seed(tmp_path, count=2)
    calls = []
    def handler(_):
        calls.append(1)
        return response([[1., 2.] if len(calls) == 1 else [1., 2., 3.]])
    result = asyncio.run(embed_pending(engine, provider(handler), apply=True, batch_size=1))
    assert result['status'] == 'partial_failure'
    assert all(row[2] == 'failed' and row[5] is None for row in rows(engine)[1:])


def test_cli_defaults_to_dry_run_and_uses_only_embedding_environment(tmp_path, capsys):
    from scripts.embed_documents import main
    engine = seed(tmp_path)
    before = rows(engine)
    assert main(['--database-url', engine.url.render_as_string(hide_password=False)], environ=configuration()) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'dry_run' and rows(engine) == before
    assert main(['--database-url', engine.url.render_as_string(hide_password=False), '--apply'], environ={'AI_API_KEY': 'chat-secret'}) == 2
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'unconfigured' and 'chat-secret' not in json.dumps(result)


@pytest.mark.parametrize('entity,field,value', [(Source, 'status', 'withdrawn'), (Source, 'owner_id', 'foreign'),
                                               (Technique, 'status', 'draft'), (Technique, 'owner_id', 'foreign')])
def test_ineligible_records_never_send_text(tmp_path, entity, field, value):
    from sqlalchemy import update
    from app.embedding_jobs import embed_pending
    engine = seed(tmp_path)
    with engine.begin() as conn:
        conn.execute(update(entity).values({field: value}))
    calls = []
    result = asyncio.run(embed_pending(engine, provider(lambda r: calls.append(r)), apply=True))
    assert result['documents'] == [] and calls == []
    assert all(row[2] == 'pending' for row in rows(engine))


@pytest.mark.parametrize('field,value', [('status', 'withdrawn'), ('owner_id', 'foreign')])
def test_source_revoked_during_request_cannot_publish(tmp_path, field, value):
    from sqlalchemy import update
    from app.embedding_jobs import embed_pending
    engine = seed(tmp_path, count=1)
    def handler(_):
        with engine.begin() as conn:
            conn.execute(update(Source).values({field: value}))
        return response([[1., 2.]])
    result = asyncio.run(embed_pending(engine, provider(handler), apply=True))
    assert result['documents'][0]['status'] == 'stale'
    assert all(row[2] == 'pending' and row[5] is None for row in rows(engine))


@pytest.mark.parametrize('texts', [[], [''], ['a'] * 65, ['a' * 12001], ['a' * 12000] * 11])
def test_embedding_input_limits_reject_before_network(texts):
    calls = []
    with pytest.raises(ProviderError):
        asyncio.run(provider(lambda r: calls.append(r)).embed(texts))
    assert calls == []


def test_cancellation_leaves_no_partial_vectors(tmp_path):
    from app.embedding_jobs import embed_pending
    engine = seed(tmp_path, count=2)
    class Interrupted:
        embedding_configured = True
        embedding_model = 'encoder-v1'
        async def embed(self, texts):
            raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(embed_pending(engine, Interrupted(), apply=True))
    assert all(row[2] == 'pending' and row[5] is None for row in rows(engine))


def test_title_only_record_embeds_real_title_and_hashes_actual_input(tmp_path):
    from app.embedding_jobs import embed_pending
    engine = create_engine(test_database_url(f'{tmp_path / "title.db"}'))
    with Session(engine) as db:
        db.add(Technique(title='靛蓝罩染', sources=[Source(title='出处')]))
        db.commit()
    calls = []
    def handler(request):
        calls.append(json.loads(request.content)['input'])
        return response([[.5, 1.]])
    result = asyncio.run(embed_pending(engine, provider(handler), apply=True))
    assert result['status'] == 'completed'
    assert calls == [['靛蓝罩染']]
    with Session(engine) as db:
        embedding = db.scalar(select(Embedding))
        assert embedding.status == 'ready' and embedding.content_hash == digest('靛蓝罩染')


def test_progress_reports_each_terminal_document(tmp_path):
    from app.embedding_jobs import embed_pending
    engine = seed(tmp_path)
    seen = []
    result = asyncio.run(embed_pending(engine, provider(lambda _: response([[1., 0.]] * 3)),
                                      apply=True, progress=lambda done, total: seen.append((done,total))))
    assert result['documents'][0]['status'] == 'ready'
    assert seen == [(0,1),(1,1)]
    assert rows(engine)[-1][2] == 'ready'


@pytest.mark.parametrize('model', ['text-embedding-v3', 'text-embedding-v4'])
@pytest.mark.parametrize('count', [10, 11, 12, 23])
def test_bailian_model_batches_at_ten_and_preserves_global_order(model, count):
    calls = []
    texts = [f'input {i}' for i in range(count)]
    def handler(request):
        batch = json.loads(request.content)['input']
        calls.append(batch)
        if len(batch) > 10:
            return httpx.Response(400, json={'error': {'message': 'maximum 10 items'}})
        indices = list(reversed(range(len(batch))))
        return response([[float(batch[index].split()[-1]), .5] for index in indices], indices)
    p = LangChainProvider(settings={**configuration(), 'EMBEDDING_MODEL': model},
                          transport=httpx.MockTransport(handler))
    assert asyncio.run(p.embed(texts)) == [[float(i), .5] for i in range(count)]
    assert calls == [texts[start:start + 10] for start in range(0, count, 10)]


def test_other_embedding_models_retain_existing_batch_capacity():
    calls = []
    def handler(request):
        batch = json.loads(request.content)['input']
        calls.append(batch)
        return response([[float(i)] for i in range(len(batch))])
    assert len(asyncio.run(provider(handler).embed(['input'] * 23))) == 23
    assert len(calls) == 1 and len(calls[0]) == 23


def test_bailian_later_request_failure_never_retries_or_returns_partial_vectors():
    calls = []
    def handler(request):
        batch = json.loads(request.content)['input']
        calls.append(batch)
        if len(batch) > 10 or len(calls) == 2:
            return httpx.Response(400, json={'error': {'message': 'private-provider-body'}})
        return response([[1., .5]] * len(batch))
    p = LangChainProvider(settings={**configuration(), 'EMBEDDING_MODEL': 'text-embedding-v4'},
                          transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as exc:
        asyncio.run(p.embed(['input'] * 23))
    assert [len(batch) for batch in calls] == [10, 10]
    assert 'HTTP 400' in str(exc.value) and 'private-provider-body' not in str(exc.value)


def test_bailian_cancellation_during_second_request_stops_remaining_batches():
    calls = []
    async def handler(request):
        batch = json.loads(request.content)['input']
        calls.append(batch)
        if len(batch) > 10:
            return httpx.Response(400, json={'error': {'message': 'maximum 10 items'}})
        if len(calls) == 2:
            raise asyncio.CancelledError()
        return response([[1., .5]] * len(batch))
    p = LangChainProvider(settings={**configuration(), 'EMBEDDING_MODEL': 'text-embedding-v4'},
                          transport=httpx.MockTransport(handler))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(p.embed(['input'] * 23))
    assert [len(batch) for batch in calls] == [10, 10]


def test_bailian_dimensions_must_match_across_sdk_batches():
    calls = []
    def handler(request):
        batch = json.loads(request.content)['input']
        calls.append(batch)
        if len(batch) > 10:
            return httpx.Response(400, json={'error': {'message': 'maximum 10 items'}})
        return response([[1.] * (2 if len(calls) == 1 else 3)] * len(batch))
    p = LangChainProvider(settings={**configuration(), 'EMBEDDING_MODEL': 'text-embedding-v4'},
                          transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError, match='不一致的维度'):
        asyncio.run(p.embed(['input'] * 11))
    assert [len(batch) for batch in calls] == [10, 1]


@pytest.mark.parametrize('failure', [None, 'http', 'dimensions', 'cancel'])
def test_bailian_sdk_sub_batches_preserve_document_atomicity_and_existing_ready(tmp_path, failure):
    from app.embedding_jobs import embed_pending
    engine = seed(tmp_path, count=23)
    with Session(engine) as db:
        ready = db.scalar(select(Embedding).join(Chunk).where(Chunk.version == 2, Chunk.ordinal == 0))
        ready.status, ready.model, ready.dimensions, ready.vector = 'ready', 'text-embedding-v4', 2, [9., 9.]
        db.commit()
    before = rows(engine)
    calls = []
    def handler(request):
        batch = json.loads(request.content)['input']
        calls.append(batch)
        assert rows(engine) == before
        assert len(batch) <= 10
        if len(calls) == 2 and failure == 'cancel':
            raise asyncio.CancelledError()
        if len(calls) == 2 and failure == 'http':
            return httpx.Response(400, json={'error': {'message': 'private-batch-error'}})
        indices = list(reversed(range(len(batch))))
        vectors = [[float(batch[index].split()[-1]), .5] for index in indices]
        if len(calls) == 2 and failure == 'dimensions':
            vectors = [vector + [1.] for vector in vectors]
        return response(vectors, indices)
    p = LangChainProvider(settings={**configuration(), 'EMBEDDING_MODEL': 'text-embedding-v4'},
                          transport=httpx.MockTransport(handler))
    if failure == 'cancel':
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(embed_pending(engine, p, apply=True))
        assert rows(engine) == before
    else:
        result = asyncio.run(embed_pending(engine, p, apply=True))
        state = rows(engine)
        assert state[:2] == before[:2]  # Prior document version and existing ready vector survive.
        if failure:
            assert result['status'] == 'partial_failure'
            assert all(row[2] == 'failed' and row[5] is None for row in state[2:])
            assert 'private-batch-error' not in json.dumps(result)
        else:
            assert result['status'] == 'completed'
            assert all(row[2] == 'ready' for row in state[2:])
            assert [row[5] for row in state[2:]] == [[float(i), .5] for i in range(1, 23)]
    assert [len(batch) for batch in calls] == ([10, 10] if failure in {'http', 'cancel'} else [10, 10, 2])
