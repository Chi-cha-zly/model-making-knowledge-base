"""Conversation coordination; all persistence is delegated to short transactions."""

import asyncio
from contextlib import aclosing, asynccontextmanager, suppress
import hashlib
import json
from time import monotonic
from ..chat_context import bounded_evidence, compose_messages
from ..errors import ProviderError
from ..prompts import SYSTEM
from ..repositories.chat import ACTIVE, ChatRepository
from .errors import ServiceError


class ChatService:
    def __init__(self, engine, provider, retrieval=None):
        self.engine, self.provider, self.retrieval = engine, provider, retrieval
        self.repository = ChatRepository(engine)
        self.lock = asyncio.Lock()
        self.tasks = {}
        self.retrieval_queries = {}
        self.system_prompt = SYSTEM

    def recover(self):
        self.repository.recover()

    @asynccontextmanager
    async def lifespan(self, app):
        self.recover()
        yield
        async with self.lock:
            for rid in list(self.tasks):
                await self.stop(rid, '服务关闭，已保留部分文本。')

    def conversations(self):
        return self.repository.conversations()

    def create_conversation(self, title):
        return self.repository.create_conversation(title)

    def detail(self, cid):
        return self.repository.detail(cid)

    def rename(self, cid, title):
        return self.repository.rename(cid, title)

    async def remove(self, cid):
        for rid in self.repository.active_ids(cid):
            await self.stop(rid)
        self.repository.remove(cid)

    def snapshot(self, rid):
        return self.repository.snapshot(rid)

    def has_active(self):
        return self.repository.has_active()

    async def stop(self, rid, reason=None):
        task = self.tasks.get(rid)
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            self.tasks.pop(rid, None)
        self.retrieval_queries.pop(rid, None)
        return self.repository.stop(rid, reason)

    def messages_for(self, db, run):
        return compose_messages(
            self.system_prompt, run.citations, self.repository.history_turns(db, run)
        )

    async def generate(self, rid):
        content = ''
        persisted = ''
        pending = None
        last_flush = monotonic()

        def flush():
            nonlocal persisted, last_flush
            if content != persisted:
                if not self.repository.flush_content(rid, content):
                    return False
                persisted = content
            last_flush = monotonic()
            return True

        try:
            query = self.retrieval_queries.get(rid)
            citations = None
            if query is not None:
                # This await belongs to the cancellable queued run, outside the API lock.
                result = await self.retrieval.search(query[0], k=6, semantic_query=query[1])
                citations = bounded_evidence(self.retrieval.revalidate(result).hits)
            run, turns = self.repository.begin_generation(rid, citations)
            content = persisted = run['content']
            messages = compose_messages(self.system_prompt, run['citations'], turns)
            async with aclosing(self.provider.stream(messages)) as stream:
                try:
                    while True:
                        if pending is None:
                            pending = asyncio.create_task(anext(stream))
                        done, _ = await asyncio.wait(
                            [pending], timeout=max(0, 0.5 - (monotonic() - last_flush))
                        )
                        if not done:
                            if not flush():
                                return
                            continue
                        completed, pending = pending, None
                        try:
                            chunk = completed.result()
                        except StopAsyncIteration:
                            break
                        if not isinstance(chunk, str):
                            raise ProviderError('模型服务返回了无法解析的文本。')
                        if len(content) + len(chunk) > 100000:
                            raise ProviderError('回答达到长度上限；请缩小问题范围后重试。')
                        content += chunk
                        if len(content) - len(persisted) >= 256 or monotonic() - last_flush >= 0.5:
                            if not flush():
                                return
                finally:
                    if pending is not None:
                        pending.cancel()
                        with suppress(asyncio.CancelledError, StopAsyncIteration):
                            await pending
            flush()
            self.repository.finish(rid)
        except asyncio.CancelledError:
            flush()
            raise
        except Exception as exc:
            flush()
            self.repository.finish(
                rid,
                str(exc)
                if isinstance(exc, ProviderError)
                else '生成失败，已保留部分文本；请检查模型服务后重试。',
            )
        finally:
            self.tasks.pop(rid, None)
            self.retrieval_queries.pop(rid, None)

    async def create_run(self, cid, data, action='start', prior_id=None):
        fingerprint = hashlib.sha256(
            json.dumps(
                {'action': action, 'prior_id': prior_id, 'content': getattr(data, 'content', None)},
                sort_keys=True,
            ).encode()
        ).hexdigest()
        state = self.repository.request_state(cid, data.request_id, prior_id)
        duplicate, latest = state['duplicate'], state['latest']
        if duplicate:
            if duplicate['request_hash'] != fingerprint:
                raise ServiceError(409, 'request_id 已用于其他请求，请使用新的标识。')
            return {
                key: value
                for key, value in duplicate.items()
                if key not in ('request_id', 'request_hash')
            }
        if not self.provider.configured:
            raise ServiceError(
                503,
                '尚未配置模型；请设置 AI_BASE_URL、AI_MODEL 和 AI_API_KEY（本地服务可省略密钥）。',
            )
        if prior_id is not None and (not latest or latest['id'] != prior_id):
            raise ServiceError(409, '只能重试或补充最新一轮。')
        if latest and latest['status'] in ACTIVE and action != 'steer':
            raise ServiceError(409, '当前会话仍在生成，请先停止。')
        if action == 'steer' and (not latest or latest['status'] not in (*ACTIVE, 'stopped')):
            raise ServiceError(409, '只能打断当前生成或补充已停止的最新一轮。')
        if action == 'steer':
            await self.stop(prior_id)
        latest_question = state['prior_content'] if action == 'retry' else data.content
        recent = state['recent'] if action == 'retry' else [data.content, *state['recent']][:4]
        query = '\n'.join(reversed(recent))[-64000:]
        citations = [] if self.retrieval is not None else self.repository.evidence(query)
        output = self.repository.create_run(cid, data, action, prior_id, fingerprint, citations)
        if self.retrieval is not None:
            self.retrieval_queries[output['id']] = (query, latest_question)
        self.tasks[output['id']] = asyncio.create_task(self.generate(output['id']))
        return output
