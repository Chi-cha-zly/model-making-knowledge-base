"""Settings activation and provider replacement under the shared activity lock."""

import asyncio
from copy import deepcopy
from time import perf_counter
from uuid import uuid4
from ..llm import LangChainProvider
from ..prompts import compose_prompt
from .errors import ServiceError


class SettingsService:
    def __init__(self, store, chat, experiments):
        self.store, self.chat, self.experiments = store, chat, experiments

    def idle(self):
        if (
            self.chat.has_active()
            or any(not t.done() for t in self.chat.tasks.values())
            or self.experiments.has_active()
        ):
            raise ServiceError(409, '请等待对话和实验完全结束后再修改设置或测试连接。')

    def apply(self, data, *, provider_change):
        values = self.store.values(data)
        provider = LangChainProvider(values) if provider_change else self.chat.provider
        self.store.save(data)
        self.chat.provider = provider
        self.experiments.provider = provider
        self.experiments.settings = values
        self.chat.system_prompt = self.experiments.system_prompt = compose_prompt(
            data['custom_prompt']
        )

    async def create(self, payload):
        async with self.chat.lock:
            self.idle()
            data = deepcopy(self.store.data)
            if len(data['profiles']) >= 20:
                raise ServiceError(409, '最多保存 20 个连接配置。')
            self.store.check_key(payload.base_url, payload.api_key)
            data['profiles'].append({'id': str(uuid4()), **payload.model_dump()})
            self.apply(data, provider_change=False)
            return self.store.view()

    async def update(self, id, payload):
        async with self.chat.lock:
            self.idle()
            data = deepcopy(self.store.data)
            old = self.store.profile(id, data)
            if payload.base_url != old['base_url'] and payload.api_key is None:
                raise ServiceError(422, '更改服务地址时必须明确提供新 API key 或清除旧 key。')
            values = payload.model_dump()
            if payload.api_key is None:
                values.pop('api_key')
                values['credential_ref'] = old.get('credential_ref')
            self.store.check_key(
                values['base_url'],
                values.get('api_key') if 'api_key' in values else values.get('credential_ref'),
            )
            old.clear()
            old.update({'id': id, **values})
            self.apply(data, provider_change=id == data['active_profile_id'])
            return self.store.view()

    async def delete(self, id):
        async with self.chat.lock:
            self.idle()
            data = deepcopy(self.store.data)
            profile = self.store.profile(id, data)
            if id == data['active_profile_id']:
                raise ServiceError(409, '请先切换到其他连接。')
            data['profiles'].remove(profile)
            self.apply(data, provider_change=False)
            return self.store.view()

    async def activate(self, id):
        async with self.chat.lock:
            self.idle()
            data = deepcopy(self.store.data)
            if id != 'env':
                self.store.profile(id, data)
            data['active_profile_id'] = id
            self.apply(data, provider_change=True)
            return self.store.view()

    async def prompt(self, payload):
        async with self.chat.lock:
            self.idle()
            data = deepcopy(self.store.data)
            data['custom_prompt'] = payload.text
            self.apply(data, provider_change=False)
            return self.store.view()

    async def test(self, id):
        async with self.chat.lock:
            self.idle()
            self.store.profile(id)
            values = self.store.values(id=id)
            values['AI_MAX_OUTPUT_TOKENS'] = '256'
            values['LANGSMITH_TRACING'] = 'false'
            provider = LangChainProvider(values)
            started = perf_counter()
            try:
                await asyncio.wait_for(
                    provider.complete(
                        [{'role': 'user', 'content': '请只回复：连接成功'}], trace=False
                    ),
                    timeout=20,
                )
                return {
                    'ok': True,
                    'message': '连接成功。',
                    'latency_ms': round((perf_counter() - started) * 1000),
                }
            except Exception:
                return {
                    'ok': False,
                    'message': '连接测试失败；请检查地址、模型、API key 和服务状态。',
                    'latency_ms': round((perf_counter() - started) * 1000),
                }
