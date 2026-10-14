from pg_test_support import test_database_url
import asyncio
import json
import threading
import time
from io import BytesIO
from zipfile import ZipFile

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.errors import ProviderError


class ControlledProvider:
    configured = True
    model = 'test-model'

    def __init__(self, fail=False):
        self.fail = fail
        self.closed = threading.Event()
        self.prompts = []

    async def stream(self, messages):
        self.prompts.append(messages)
        try:
            yield '第一段'
            if self.fail:
                raise RuntimeError('secret upstream failure')
            await asyncio.sleep(3600)
        finally:
            self.closed.set()


def setup(tmp_path, provider=None):
    return create_app(test_database_url(f"{tmp_path / 'chat.db'}"), tmp_path / 'uploads', provider=provider or ControlledProvider())


def conversation(client):
    response = client.post('/api/v1/conversations', json={})
    assert response.status_code == 201, response.text
    return response.json()['id']


def start(client, cid, request='one', content='如何修水口？'):
    return client.post(f'/api/v1/conversations/{cid}/runs', json={'content': content, 'request_id': request})


def wait(client, rid, status=None):
    for _ in range(200):
        row = client.get(f'/api/v1/runs/{rid}').json()
        if (status and row['status'] == status) or (not status and row['content']):
            return row
        time.sleep(.01)
    raise AssertionError(row)


def test_cancel_dedupe_retry_and_steer(tmp_path):
    provider = ControlledProvider()
    with TestClient(setup(tmp_path, provider)) as client:
        cid = conversation(client)
        run = start(client, cid).json()
        wait(client, run['id'])
        assert start(client, cid).json()['id'] == run['id']
        assert start(client, cid, content='different').status_code == 409
        assert start(client, cid, request='two').status_code == 409
        stopped = client.post(f"/api/v1/runs/{run['id']}/stop").json()
        assert stopped['status'] == 'stopped' and stopped['content'] == '第一段'
        assert provider.closed.wait(1), 'stop must close the upstream iterator'
        retry = client.post(f"/api/v1/runs/{run['id']}/retry", json={'request_id': 'retry'}).json()
        assert client.post(f"/api/v1/runs/{run['id']}/retry", json={'request_id': 'retry'}).json()['id'] == retry['id']
        wait(client, retry['id'])
        steer = client.post(f"/api/v1/runs/{retry['id']}/steer", json={'content': '只有砂纸', 'request_id': 'steer'}).json()
        assert client.post(f"/api/v1/runs/{retry['id']}/steer", json={'content': '只有砂纸', 'request_id': 'steer'}).json()['id'] == steer['id']
        assert client.get(f"/api/v1/runs/{retry['id']}").json()['status'] == 'stopped'
        assert client.post(f"/api/v1/runs/{run['id']}/retry", json={'request_id': 'old'}).status_code == 409
        history = client.get(f'/api/v1/conversations/{cid}').json()
        assert [m['content'] for m in history['messages'] if m['role'] == 'user'] == ['如何修水口？', '只有砂纸']
        assert len(history['runs']) == 3
        client.post(f"/api/v1/runs/{steer['id']}/stop")
        events = client.get(f"/api/v1/runs/{steer['id']}/events").text
        assert 'event: snapshot' in events and '"status": "stopped"' in events


def test_failure_is_sanitized_and_partial_output_persists(tmp_path):
    with TestClient(setup(tmp_path, ControlledProvider(fail=True))) as client:
        cid = conversation(client)
        run = start(client, cid).json()
        row = wait(client, run['id'], 'failed')
        assert row['content'] == '第一段'
        assert row['error'] and 'secret' not in row['error']


def test_evidence_only_published_local_with_published_sources(tmp_path):
    provider = ControlledProvider()
    with TestClient(setup(tmp_path, provider)) as client:
        sid = client.post('/api/v1/sources', json={'title': '出处', 'url': 'https://example.org'}).json()['id']
        for title, status in [('修水口', 'published'), ('修水口秘密', 'draft')]:
            client.post('/api/v1/techniques', json={'title': title, 'summary': '先剪后磨', 'source_ids': [sid], 'status': status})
        cid = conversation(client)
        run = start(client, cid).json()
        row = wait(client, run['id'])
        assert len(row['citations']) == 1
        assert row['citations'][0]['title'] == '修水口'
        assert row['citations'][0]['source_ids'] == [sid]
        assert '修水口秘密' not in json.dumps(provider.prompts, ensure_ascii=False)
        client.post(f"/api/v1/runs/{run['id']}/stop")
        client.patch(f'/api/v1/sources/{sid}', json={'status': 'withdrawn'})
        run = start(client, cid, 'second').json()
        assert wait(client, run['id'])['citations'] == []


def test_backup_roundtrip_and_v1_preserves_chat(tmp_path):
    with TestClient(setup(tmp_path)) as client:
        cid = conversation(client)
        run = start(client, cid).json()
        wait(client, run['id'])
        assert client.post('/api/v1/backup', files={'file': ('x.zip', b'x')}).status_code == 409
        client.post(f"/api/v1/runs/{run['id']}/stop")
        backup = client.get('/api/v1/backup').content
        assert client.delete(f'/api/v1/conversations/{cid}').status_code == 204
        assert client.post('/api/v1/backup', files={'file': ('backup.zip', backup)}).status_code == 200
        assert client.get(f'/api/v1/conversations/{cid}').json()['runs'][0]['content'] == '第一段'
        manifest = json.loads(ZipFile(BytesIO(backup)).read('manifest.json'))
        manifest['version'] = 1
        for key in ('conversations', 'messages', 'runs'):
            manifest.pop(key)
        legacy = BytesIO()
        with ZipFile(legacy, 'w') as archive:
            archive.writestr('manifest.json', json.dumps(manifest))
        assert client.post('/api/v1/backup', files={'file': ('old.zip', legacy.getvalue())}).status_code == 200
        assert client.get(f'/api/v1/conversations/{cid}').status_code == 200


def test_shutdown_and_restart_recover_run(tmp_path):
    app = setup(tmp_path)
    with TestClient(app) as client:
        cid = conversation(client)
        run = start(client, cid).json()
        wait(client, run['id'])
    with TestClient(setup(tmp_path)) as client:
        assert client.get(f"/api/v1/runs/{run['id']}").json()['status'] == 'stopped'


def test_provider_parses_sse_and_rejects_truncation():
    from app.llm import LangChainProvider

    async def collect(body):
        requests = []
        def handle(request):
            requests.append(request)
            return httpx.Response(200, text=body)
        provider = LangChainProvider(settings={'AI_BASE_URL': 'http://localhost:1234/v1', 'AI_MODEL': 'local'}, transport=httpx.MockTransport(handle))
        parts = [part async for part in provider.stream([{'role': 'user', 'content': 'hello'}])]
        assert json.loads(requests[0].content)['stream'] is True
        assert str(requests[0].url) == 'http://localhost:1234/v1/chat/completions'
        return parts
    assert asyncio.run(collect(': keepalive\n\ndata: {"choices":[{"delta":{"role":"assistant"}}]}\n\ndata: {"choices":[{"delta":{"content":"你好"}}]}\n\ndata: [DONE]\n\n')) == ['你好']
    with pytest.raises(ProviderError, match='中断|incomplete|truncated'):
        asyncio.run(collect('data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'))


def test_concurrent_requests_and_queued_stop_are_serialized(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    with TestClient(setup(tmp_path)) as client:
        cid = conversation(client)
        with ThreadPoolExecutor(max_workers=6) as pool:
            responses = list(pool.map(lambda _: start(client, cid), range(6)))
        assert {r.status_code for r in responses} == {201}
        assert len({r.json()['id'] for r in responses}) == 1
        run = responses[0].json()
        client.post(f"/api/v1/runs/{run['id']}/stop")
        history = client.get(f'/api/v1/conversations/{cid}').json()
        assert len(history['messages']) == 2 and len(history['runs']) == 1
        assert client.post(f'/api/v1/conversations/{cid}/runs', json={'content': 'x' * 16001, 'request_id': 'long'}).status_code == 422


def test_real_httpx_stream_is_closed_on_stop(tmp_path):
    from app.llm import LangChainProvider
    closed = threading.Event()
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            await asyncio.sleep(3600)
        async def aclose(self):
            closed.set()
    provider = LangChainProvider(settings={'AI_BASE_URL': 'http://localhost:1234/v1', 'AI_MODEL': 'local'},
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=SlowStream())))
    with TestClient(setup(tmp_path, provider)) as client:
        run = start(client, conversation(client)).json()
        wait(client, run['id'])
        stopped = client.post(f"/api/v1/runs/{run['id']}/stop").json()
        assert stopped['content'] == 'partial' and stopped['status'] == 'stopped'
        assert closed.wait(1), 'cancellation must close HTTPX upstream response'


def test_orphan_recovery_and_terminal_safe_backup(tmp_path):
    from sqlalchemy.orm import Session
    from app.chat_models import Run
    app = setup(tmp_path)
    with TestClient(app) as client:
        run = start(client, conversation(client)).json()
        wait(client, run['id'])
        manifest = json.loads(ZipFile(BytesIO(client.get('/api/v1/backup').content)).read('manifest.json'))
        assert manifest['runs'][0]['status'] == 'stopped'
        assert client.get(f"/api/v1/runs/{run['id']}").json()['status'] == 'running'
    with Session(app.state.chat.engine) as db:
        db.get(Run, run['id']).status = 'running'  # Simulate abrupt process loss.
        db.commit()
    with TestClient(setup(tmp_path)) as client:
        recovered = client.get(f"/api/v1/runs/{run['id']}").json()
        assert recovered['status'] == 'stopped' and '重启' in recovered['error']
        assert recovered['content'] == '第一段'


def test_empty_provider_and_http_errors_are_visible_without_secrets(tmp_path):
    from app.llm import LangChainProvider
    provider = LangChainProvider(settings={'AI_BASE_URL': 'https://example.org/v1', 'AI_API_KEY': 'secret-key', 'AI_MODEL': 'model'},
        transport=httpx.MockTransport(lambda request: httpx.Response(401, text='secret-key raw response')))
    with TestClient(setup(tmp_path, provider)) as client:
        assert 'secret' not in client.get('/api/v1/agent/config').text
        run = start(client, conversation(client)).json()
        result = wait(client, run['id'], 'failed')
        assert '401' in result['error'] and 'secret' not in result['error']
    with TestClient(setup(tmp_path, LangChainProvider(settings={}))) as client:
        assert client.get('/api/v1/agent/config').json()['configured'] is False
        cid = conversation(client)
        assert start(client, cid).status_code == 503
        assert client.get(f'/api/v1/conversations/{cid}').json()['messages'] == []


def test_completed_retry_context_ignores_stopped_draft(tmp_path):
    class FinishProvider:
        configured = True
        model = 'test'
        def __init__(self):
            self.prompts = []
        async def stream(self, messages):
            self.prompts.append(messages)
            yield '已完成'
    provider = FinishProvider()
    with TestClient(setup(tmp_path, provider)) as client:
        cid = conversation(client)
        run = start(client, cid).json()
        wait(client, run['id'], 'completed')
        retried = client.post(f"/api/v1/runs/{run['id']}/retry", json={'request_id': 'retry'}).json()
        wait(client, retried['id'], 'completed')
        assert all(m['role'] != 'assistant' for m in provider.prompts[-1])
        start(client, cid, 'next', '下一步')
        for _ in range(100):
            if len(provider.prompts) == 3:
                break
            time.sleep(.01)
        assert [m['content'] for m in provider.prompts[-1] if m['role'] == 'assistant'] == ['已完成']


def test_prompt_history_has_total_character_budget(tmp_path):
    from app.chat_models import Conversation, Message, Run
    from sqlalchemy.orm import Session
    app = setup(tmp_path)
    with Session(app.state.chat.engine) as db:
        c = Conversation(title='Long context')
        db.add(c)
        db.flush()
        for i in range(20):
            user = Message(conversation_id=c.id, role='user', content=str(i) + 'u' * 15000)
            assistant = Message(conversation_id=c.id, role='assistant', content='a' * 24000)
            db.add_all([user, assistant])
            db.flush()
            run = Run(conversation_id=c.id, user_message_id=user.id, assistant_message_id=assistant.id,
                      status='completed', content=assistant.content, citations=[], request_id=str(i), request_hash='a' * 64)
            db.add(run)
        db.flush()
        messages = app.state.chat.messages_for(db, run)
        assert sum(len(m['content']) for m in messages) < 65000
        assert any(m['content'] == user.content for m in messages)


def test_invalid_chat_backup_is_rejected_without_touching_history(tmp_path):
    with TestClient(setup(tmp_path)) as client:
        cid = conversation(client)
        run = start(client, cid).json()
        wait(client, run['id'])
        client.post(f"/api/v1/runs/{run['id']}/stop")
        backup = client.get('/api/v1/backup').content
        data = json.loads(ZipFile(BytesIO(backup)).read('manifest.json'))
        data['runs'][0]['citations'] = [{'type': 'technique', 'id': 1, 'title': 'x', 'excerpt': 'x', 'source_ids': [], 'sources': [None]}]
        stream = BytesIO()
        with ZipFile(stream, 'w') as archive:
            archive.writestr('manifest.json', json.dumps(data))
        response = client.post('/api/v1/backup', files={'file': ('bad.zip', stream.getvalue())})
        assert response.status_code == 400
        assert client.get(f'/api/v1/conversations/{cid}').json()['runs'][0]['content'] == '第一段'


def test_citation_excerpt_is_readable_and_omits_empty_fields(tmp_path):
    with TestClient(setup(tmp_path)) as client:
        sid = client.post('/api/v1/sources', json={'title': '说明书'}).json()['id']
        client.post('/api/v1/techniques', json={
            'title': '修水口', 'summary': '先剪后磨', 'content_blocks': [{'type': 'paragraph', 'text': t} for t in ['二次剪切', '逐级打磨']],
            'source_ids': [sid], 'tags': ['素组', '水口']})
        client.post('/api/v1/products', json={
            'name': '水口砂纸', 'kind': '打磨工具', 'aliases': ['打磨纸'],
            'properties': {'目数': ['600', '1000'], '备注': None, '其他': {}, '防水': False},
            'source_ids': [sid]})
        run = start(client, conversation(client)).json()
        row = wait(client, run['id'])
        excerpts = {citation['type']: citation['excerpt'] for citation in row['citations']}
        assert '摘要：先剪后磨' in excerpts['technique']
        assert '二次剪切\n逐级打磨' in excerpts['technique']
        assert '标签：\n- 素组\n- 水口' in excerpts['technique']
        assert '前提条件' not in excerpts['technique']
        assert '类别：打磨工具' in excerpts['product']
        assert '目数：\n- 600\n- 1000' in excerpts['product']
        assert '防水：否' in excerpts['product']
        for excerpt in excerpts.values():
            assert 'null' not in excerpt and '{' not in excerpt
            assert '备注' not in excerpt and '其他' not in excerpt
            assert len(excerpt) <= 3000


def test_chat_evidence_uses_persisted_search_without_corpus_loading(tmp_path, monkeypatch):
    import app.chat as chat_module
    import app.retrieval as retrieval_module

    def forbidden(*args, **kwargs):
        raise AssertionError('online chat must not load the full corpus')

    with TestClient(setup(tmp_path)) as client:
        sid = client.post('/api/v1/sources', json={'title': '水口手册'}).json()['id']
        tid = client.post('/api/v1/techniques', json={
            'title': '水口处理', 'summary': '二次剪切后打磨', 'source_ids': [sid]}).json()['id']
        monkeypatch.setattr(retrieval_module, 'load_documents', forbidden)
        monkeypatch.setattr(chat_module, 'load_documents', forbidden, raising=False)
        run = start(client, conversation(client)).json()
        result = wait(client, run['id'])
        citation = result['citations'][0]
        assert citation['type'] == 'technique' and citation['id'] == tid
        assert citation['source_ids'] == [sid]
        assert citation['title'] == '水口处理'
        assert '二次剪切后打磨' in citation['excerpt']
