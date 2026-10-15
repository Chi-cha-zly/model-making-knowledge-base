"""Queued retrieval is cancellable and never owns a database transaction."""

import asyncio
from types import SimpleNamespace

from app.chat import ChatService, TurnInput
from app.db import create_db_engine
from pg_test_support import test_database_url


class SlowRetrieval:
    def __init__(self, engine):
        self.engine = engine
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.validations = 0

    async def search(self, query, **kwargs):
        assert self.engine.pool.checkedout() == 0
        if query == 'slow':
            self.entered.set()
            try:
                await asyncio.sleep(3600)
            finally:
                self.cancelled.set()
        return SimpleNamespace(hits=[])

    def revalidate(self, result):
        self.validations += 1
        return result


class Stream:
    configured = True
    model = 'fake'

    def __init__(self):
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.prompts = []

    async def stream(self, messages):
        self.prompts.append(messages)
        self.started.set()
        try:
            yield 'tail'
            await asyncio.sleep(3600)
        finally:
            self.cancelled.set()


def test_slow_retrieval_does_not_block_stop_and_cancels_before_generation():
    async def scenario():
        engine = create_db_engine(test_database_url('queued retrieval'))
        provider = Stream()
        retrieval = SlowRetrieval(engine)
        service = ChatService(engine, provider, retrieval=retrieval)
        first = service.create_conversation('first')['id']
        second = service.create_conversation('second')['id']
        try:
            async with service.lock:
                running = await service.create_run(first, TurnInput(content='fast', request_id='a'))
            await asyncio.wait_for(provider.started.wait(), 1)
            async with service.lock:
                queued = await asyncio.wait_for(
                    service.create_run(second, TurnInput(content='slow', request_id='b')), 0.2
                )
            await asyncio.wait_for(retrieval.entered.wait(), 1)
            assert service.snapshot(queued['id'])['status'] == 'queued'
            async with service.lock:
                replay = await service.create_run(second, TurnInput(content='slow', request_id='b'))
            assert replay['id'] == queued['id']
            assert len(service.detail(second)['messages']) == 2

            async def stop(id):
                async with service.lock:
                    return await service.stop(id)

            assert (await asyncio.wait_for(stop(running['id']), 0.2))['status'] == 'stopped'
            assert provider.cancelled.is_set()
            assert (await asyncio.wait_for(stop(queued['id']), 0.2))['status'] == 'stopped'
            assert retrieval.cancelled.is_set()
            assert len(provider.prompts) == 1
            assert service.snapshot(running['id'])['content'] == 'tail'
            assert engine.pool.checkedout() == 0
        finally:
            for rid in list(service.tasks):
                await service.stop(rid)
            engine.dispose()

    asyncio.run(scenario())


def test_retrieval_revalidates_before_citations_or_model_context_are_published():
    async def scenario():
        engine = create_db_engine(test_database_url('revalidated evidence'))
        provider = Stream()
        query_seen = []
        validations = []
        stale = {
            'type': 'technique',
            'id': 1,
            'title': 'withdrawn',
            'source_ids': [1],
            'sources': [{'id': 1, 'title': 'private'}],
            'excerpt': 'must not escape',
        }

        class Retrieval:
            async def search(self, query, **kwargs):
                assert engine.pool.checkedout() == 0
                query_seen.append((query, kwargs['semantic_query']))
                await asyncio.sleep(0)
                return SimpleNamespace(hits=[stale])

            def revalidate(self, result):
                assert result.hits == [stale]
                snapshot = service.detail(cid)['runs'][-1]
                assert snapshot['status'] == 'queued' and snapshot['citations'] == []
                validations.append(True)
                return SimpleNamespace(hits=[])

        service = ChatService(engine, provider, retrieval=Retrieval())
        cid = service.create_conversation('revalidate')['id']
        try:
            from sqlalchemy.orm import Session
            from app.chat_models import Message

            with Session(engine) as db:
                db.add_all(
                    [
                        Message(conversation_id=cid, role='user', content='old' + 'z' * 15997)
                        for _ in range(3)
                    ]
                )
                db.commit()
            question = 'prefix' + 'x' * 15988 + 'suffix'
            async with service.lock:
                run = await service.create_run(cid, TurnInput(content=question, request_id='a'))
            assert run['citations'] == []
            await asyncio.wait_for(provider.started.wait(), 1)
            assert len(query_seen[0][0]) == 64000
            assert query_seen[0][0].endswith(question)
            assert query_seen[0][1] == question
            assert validations == [True]
            assert service.snapshot(run['id'])['citations'] == []
            assert 'withdrawn' not in str(provider.prompts)
            await service.stop(run['id'])
        finally:
            for rid in list(service.tasks):
                await service.stop(rid)
            engine.dispose()

    asyncio.run(scenario())
