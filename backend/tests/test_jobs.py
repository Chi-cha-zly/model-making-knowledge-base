import asyncio
import pytest
from app.jobs import JobService
from test_chat import setup


def test_shared_jobs_progress_dedupe_and_cancel(tmp_path):
    app = setup(tmp_path)
    async def scenario():
        service = JobService(app.state.engine)
        started = asyncio.Event()
        async def work(context):
            context.progress(1, 2)
            started.set()
            await asyncio.sleep(3600)
        first = await service.submit('ingestion', 'one', {'x': 1}, work)
        await started.wait()
        assert service.snapshot(first['id'])['progress'] == 1
        again = await service.submit('ingestion', 'one', {'x': 1}, work)
        assert again['id'] == first['id']
        with pytest.raises(Exception) as error:
            await service.submit('ingestion', 'one', {'x': 2}, work)
        assert error.value.status_code == 409
        result = await service.cancel(first['id'])
        assert result['status'] == 'stopped'
        assert not service.tasks
    asyncio.run(scenario())


def test_failed_jobs_are_sanitized_and_restart_marks_interrupted(tmp_path):
    app = setup(tmp_path)
    async def scenario():
        service = JobService(app.state.engine)
        async def bad(context):
            raise RuntimeError('secret-key')
        first = await service.submit('vision', 'failure', {}, bad)
        await asyncio.gather(*list(service.tasks.values()))
        assert service.snapshot(first['id'])['status'] == 'failed'
        assert 'secret' not in str(service.snapshot(first['id']))
        async def work(context):
            await asyncio.sleep(3600)
        second = await service.submit('ingestion', 'interrupted', {}, work)
        service.recover()
        assert service.snapshot(second['id'])['status'] == 'stopped'
        await service.shutdown()
    asyncio.run(scenario())


def test_cancel_before_worker_starts_updates_callback_and_registry(tmp_path):
    app = setup(tmp_path)
    async def scenario():
        service = JobService(app.state.engine)
        calls = []
        async def work(context):
            calls.append('started')
        row = await service.submit('experiment', 'queued', {}, work, on_cancel=lambda: calls.append('stopped'))
        stopped = await service.cancel(row['id'])
        assert stopped['status'] == 'stopped'
        assert calls == ['stopped']
        assert service.tasks == {} and service.cancel_callbacks == {}
    asyncio.run(scenario())
