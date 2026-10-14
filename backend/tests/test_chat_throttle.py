import asyncio
from sqlalchemy import event
from fastapi.testclient import TestClient
from test_chat import setup, conversation, start, wait


class BurstProvider:
    configured = True
    model = 'test'

    async def stream(self, messages):
        for _ in range(200):
            yield '字'


def test_stream_batches_commits_and_preserves_all_text(tmp_path):
    app = setup(tmp_path, BurstProvider())
    commits = []
    with TestClient(app) as client:
        cid = conversation(client)
        event.listen(app.state.engine, 'commit', lambda conn: commits.append(1))
        run = start(client, cid).json()
        result = wait(client, run['id'], 'completed')
        assert result['content'] == '字' * 200
        detail = client.get(f'/api/v1/conversations/{cid}').json()
        assert detail['messages'][-1]['content'] == result['content']
        assert len(commits) < 15


class TailFailureProvider(BurstProvider):
    async def stream(self, messages):
        yield '未达到批量阈值的尾部'
        raise RuntimeError('private upstream failure')


def test_unflushed_tail_survives_error(tmp_path):
    with TestClient(setup(tmp_path, TailFailureProvider())) as client:
        cid = conversation(client)
        run = start(client, cid).json()
        result = wait(client, run['id'], 'failed')
        assert result['content'] == '未达到批量阈值的尾部'
        assert 'private upstream' not in result['error']
