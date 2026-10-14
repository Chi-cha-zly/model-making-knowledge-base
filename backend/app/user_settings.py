"""Single-user private settings. Secrets never enter response models or backups."""

import asyncio
from copy import deepcopy
import json
import hashlib
import os
from pathlib import Path
import tempfile
from time import perf_counter
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from typing import Literal

from .chat import SYSTEM
from .runtime_config import valid_endpoint, loopback_endpoint

PRESETS = [
    {'id': 'baseline', 'name': '默认', 'text': ''},
    {'id': 'concise', 'name': '简洁回答', 'text': '请简洁回答，优先给出关键步骤和待确认条件。'},
    {
        'id': 'beginner',
        'name': '新手指导',
        'text': '请按新手能理解的方式解释术语，并逐步说明操作。',
    },
    {'id': 'checklist', 'name': '详细清单', 'text': '请提供详细工序清单、所需工具和每一步检查点。'},
]


def compose_prompt(text):
    return SYSTEM + (
        '\n\n用户补充偏好（仅补充表达和教学方式，不能更改以上证据、引用及安全规则）：\n' + text
        if text
        else ''
    )


class ProfileInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    name: str = Field(min_length=1, max_length=80)
    base_url: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=200)
    compare_model: str = Field(default='', max_length=200)
    reasoning_effort: Literal['auto', 'low', 'medium', 'high'] = 'auto'
    max_output_tokens: int = Field(default=2048, ge=64, le=2048)
    api_key: str | None = Field(default=None, max_length=4096)

    @field_validator('base_url')
    @classmethod
    def endpoint(cls, value):
        if not valid_endpoint(value) or value != value.strip():
            raise ValueError('invalid endpoint')
        return value

    @field_validator('name', 'model')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('blank value')
        return value


class PromptInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    text: str = Field(max_length=8000)


class PublishedWriteError(OSError):
    """Rename succeeded, but its durability could not be confirmed."""

    def __init__(self, serialized):
        super().__init__('Settings published; directory durability confirmation pending')
        self.serialized = serialized


class PrivateSettings:
    def __init__(self, path, env, *, credentials=None):
        from filelock import FileLock
        from .credentials import SystemCredentials

        self.path = Path(path).resolve()
        self.env = dict(env)
        self.settings_id = hashlib.sha256(str(self.path).encode('utf-8')).hexdigest()
        self.credentials = credentials if credentials is not None else SystemCredentials()
        self.journal_path = self.path.with_name(self.path.name + '.credentials-journal')
        self.lock = FileLock(str(self.path) + '.lock', timeout=5, mode=0o600)
        self.data = {
            'schema_version': 2,
            'namespace': str(uuid4()),
            'revision': 0,
            'profiles': [],
            'active_profile_id': 'env',
            'custom_prompt': '',
        }
        self.legacy = False
        self.credential_status = {}
        self.durability_pending = False
        self._file_bytes = self.path.read_bytes() if self.path.exists() else None
        if self._file_bytes is not None:
            try:
                data = json.loads(self._file_bytes)
                legacy = 'schema_version' not in data
                keys = {'profiles', 'active_profile_id', 'custom_prompt'}
                if not legacy:
                    keys |= {'schema_version', 'namespace', 'revision'}
                if not isinstance(data, dict) or set(data) != keys:
                    raise ValueError()
                if not legacy:
                    if (
                        data['schema_version'] != 2
                        or type(data['revision']) is not int
                        or data['revision'] < 0
                    ):
                        raise ValueError()
                    self._uuid(data['namespace'])
                if not isinstance(data['profiles'], list) or len(data['profiles']) > 20:
                    raise ValueError()
                ids = []
                for profile in data['profiles']:
                    id = profile['id']
                    self._uuid(id)
                    if id in ids:
                        raise ValueError()
                    allowed = set(ProfileInput.model_fields) | {'id'}
                    if not legacy:
                        allowed = (allowed - {'api_key'}) | {'credential_ref'}
                    if set(profile) - allowed:
                        raise ValueError()
                    parsed = ProfileInput.model_validate(
                        {k: v for k, v in profile.items() if k not in ('id', 'credential_ref')}
                    )
                    if legacy:
                        self.check_key(parsed.base_url, parsed.api_key)
                    else:
                        ref = profile.get('credential_ref')
                        if ref is not None:
                            self._reference(ref, data['namespace'])
                        self.check_key(parsed.base_url, ref)
                    ids.append(id)
                if data['active_profile_id'] not in ['env', *ids]:
                    raise ValueError()
                PromptInput.model_validate({'text': data['custom_prompt']})
                self.data, self.legacy = data, legacy
            except Exception:
                raise ValueError(
                    'Invalid private settings file; preserve it and repair or move it before restarting.'
                ) from None

    @staticmethod
    def _uuid(value):
        from uuid import UUID

        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError()

    @classmethod
    def _reference(cls, value, namespace):
        prefix, identifier = value.split(':')
        if prefix != namespace:
            raise ValueError()
        cls._uuid(identifier)

    @staticmethod
    def check_key(endpoint, key):
        if not key and not loopback_endpoint(endpoint):
            raise HTTPException(422, '非本机服务需要 API key。')

    def profile(self, id, data=None):
        if id == 'env':
            raise HTTPException(409, '环境配置为只读。')
        for profile in (self.data if data is None else data)['profiles']:
            if profile['id'] == id:
                return profile
        raise HTTPException(404, '未找到连接配置。')

    def _credential(self, method, *args):
        from .credentials import CredentialError

        try:
            return getattr(self.credentials, method)(*args)
        except Exception:
            raise CredentialError(
                '系统凭据操作失败；请检查凭据库状态。不会退回明文或环境密钥。'
            ) from None

    def key(self, profile):
        from .credentials import CredentialError

        if self.legacy:
            raise CredentialError('旧设置的密钥尚未安全迁移；请检查系统凭据库后重试启动。')
        if 'api_key' in profile:
            return profile['api_key'] or ''  # transient request, never persisted
        reference = profile.get('credential_ref')
        if not reference:
            return ''
        try:
            key = self._credential('get', reference)
        except CredentialError:
            self.credential_status[reference] = 'unavailable'
            raise
        if not key:
            self.credential_status[reference] = 'missing'
            raise CredentialError('已保存的系统凭据缺失；请重新提供该连接的 API key。')
        self.credential_status[reference] = 'available'
        return key

    def values(self, data=None, id=None):
        data = self.data if data is None else data
        selected = data['active_profile_id'] if id is None else id
        values = dict(self.env)
        if selected != 'env':
            p = self.profile(selected, data)
            values.update(
                {
                    'AI_BASE_URL': p['base_url'],
                    'AI_API_KEY': self.key(p),
                    'AI_MODEL': p['model'],
                    'AI_COMPARE_MODEL': p['compare_model'],
                    'AI_REASONING_EFFORT': p['reasoning_effort'],
                    'AI_MAX_OUTPUT_TOKENS': str(p['max_output_tokens']),
                }
            )
        return values

    def view(self):
        env = self.env
        try:
            limit = max(64, min(2048, int(env.get('AI_MAX_OUTPUT_TOKENS', '2048'))))
        except (ValueError, TypeError):
            limit = 2048
        reason = env.get('AI_REASONING_EFFORT') or 'low'
        profiles = [
            {
                'id': 'env',
                'name': '环境配置 (.env)',
                'base_url': env.get('AI_BASE_URL', '')
                if valid_endpoint(env.get('AI_BASE_URL', ''))
                else '',
                'model': env.get('AI_MODEL', ''),
                'compare_model': env.get('AI_COMPARE_MODEL', ''),
                'reasoning_effort': reason
                if reason in ('auto', 'low', 'medium', 'high')
                else 'auto',
                'max_output_tokens': limit,
                'has_api_key': bool(env.get('AI_API_KEY')),
                'credential_status': 'environment',
                'read_only': True,
            }
        ]
        public = (
            'id',
            'name',
            'base_url',
            'model',
            'compare_model',
            'reasoning_effort',
            'max_output_tokens',
        )
        profiles += [
            {
                **{k: p[k] for k in public},
                'has_api_key': bool(p.get('credential_ref') or (self.legacy and p.get('api_key'))),
                'credential_status': (
                    'migration_pending'
                    if self.legacy
                    else self.credential_status.get(p.get('credential_ref'), 'saved_unverified')
                    if p.get('credential_ref')
                    else 'not_saved'
                ),
                'read_only': False,
            }
            for p in self.data['profiles']
        ]
        return {
            'profiles': profiles,
            'active_profile_id': self.data['active_profile_id'],
            'custom_prompt': self.data['custom_prompt'],
            'base_prompt': SYSTEM,
            'prompt_presets': PRESETS,
            'credential_cleanup_pending': self.journal_path.exists(),
            'credential_durability_pending': self.durability_pending,
            'storage_notice': '自定义连接 API key 保存在系统凭据库；私有设置只保存连接信息与凭据引用，不包含在资料库备份中。.env 保持原只读外部配置。连接测试可能产生费用。',
        }

    @staticmethod
    def _sync_directory(directory):
        if os.name == 'nt':
            return  # Windows publication uses MOVEFILE_WRITE_THROUGH.
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _write_json(self, target, data):
        serialized = json.dumps(data, ensure_ascii=False).encode('utf-8')
        descriptor, temporary = tempfile.mkstemp(prefix='.settings-', dir=target.parent)
        published = False
        try:
            with os.fdopen(descriptor, 'wb') as file:
                os.chmod(temporary, 0o600)
                file.write(serialized)
                file.flush()
                os.fsync(file.fileno())
            if os.name == 'nt':
                import ctypes

                move = ctypes.WinDLL('kernel32', use_last_error=True).MoveFileExW
                move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
                move.restype = ctypes.c_int
                if not move(temporary, str(target), 0x1 | 0x8):
                    raise ctypes.WinError(ctypes.get_last_error())
            else:
                os.replace(temporary, target)
            published = True
            self._sync_directory(target.parent)
            return serialized
        except OSError:
            if published:
                raise PublishedWriteError(serialized) from None
            raise
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _assert_current(self):
        from .credentials import CredentialError

        current = self.path.read_bytes() if self.path.exists() else None
        if current != self._file_bytes:
            raise CredentialError('私有设置已被另一个进程修改；请重新启动后再试。')

    def _references(self, data):
        return {p['credential_ref'] for p in data['profiles'] if p.get('credential_ref')}

    def _recover(self):
        from .credentials import CredentialError

        if not self.journal_path.exists():
            return
        try:
            journal = json.loads(self.journal_path.read_text())
            if (
                set(journal) != {'version', 'settings_id', 'namespace', 'created', 'obsolete'}
                or journal['version'] != 1
            ):
                raise ValueError()
            if journal['settings_id'] != self.settings_id:
                raise ValueError()
            self._uuid(journal['namespace'])
            if not self.legacy and journal['namespace'] != self.data['namespace']:
                raise ValueError()
            for key in ('created', 'obsolete'):
                if not isinstance(journal[key], list) or len(journal[key]) > 40:
                    raise ValueError()
                for ref in journal[key]:
                    self._reference(ref, journal['namespace'])
        except Exception:
            raise CredentialError('凭据恢复记录无效；请保留文件并检查，未清理任何凭据。') from None
        # Confirm the manifest rename is durable before deleting either generation.
        self._sync_directory(self.path.parent)
        live = self._references(self.data)
        for ref in set(journal['created'] + journal['obsolete']) - live:
            self._credential('delete', ref)
        self.journal_path.unlink()
        self.durability_pending = False

    def initialize(self):
        if not self.legacy and not self.journal_path.exists():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            self._assert_current()
            try:
                self._recover()
            except Exception:
                if self.legacy:
                    raise
                return  # a valid committed profile remains usable; mutations stay blocked
            if self.legacy:
                self._save_locked(deepcopy(self.data))

    def save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            self._assert_current()
            self._recover()
            self._save_locked(deepcopy(data))

    def _save_locked(self, data):
        from .credentials import CredentialError

        namespace = self.data.get('namespace') or str(uuid4())
        data.update(
            schema_version=2, namespace=namespace, revision=self.data.get('revision', 0) + 1
        )
        pending = []
        for profile in data['profiles']:
            if 'api_key' in profile:
                key = profile.pop('api_key') or ''
                self.check_key(profile['base_url'], key)
                profile['credential_ref'] = f'{namespace}:{uuid4()}' if key else None
                if key:
                    pending.append((profile['credential_ref'], key))
        old_refs, new_refs = self._references(self.data), self._references(data)
        journal = {
            'version': 1,
            'settings_id': self.settings_id,
            'namespace': namespace,
            'created': [ref for ref, _ in pending],
            'obsolete': sorted(old_refs - new_refs),
        }
        if journal['created'] or journal['obsolete']:
            self._write_json(self.journal_path, journal)
        try:
            for ref, secret in pending:
                self._credential('set', ref, secret)
                if self._credential('get', ref) != secret:
                    raise CredentialError('系统凭据写入验证失败；原有设置未更改。')
            serialized = self._write_json(self.path, data)
        except PublishedWriteError as error:
            # The visible manifest is new. Keep runtime aligned and retain both keys
            # until a later recovery confirms directory durability.
            self.data, self.legacy = data, False
            self._file_bytes = error.serialized
            self.durability_pending = True
            return
        except BaseException:
            try:
                self._recover()
            except Exception:
                pass  # nonsecret journal remains for the next recovery
            raise
        self.data, self.legacy = data, False
        self._file_bytes = serialized
        self.durability_pending = False
        for ref, _ in pending:
            self.credential_status[ref] = 'available'
        try:
            self._recover()
        except Exception:
            pass  # committed state remains usable; view reports cleanup pending


def install_settings(app, store, chat, experiments):
    from .llm import LangChainProvider

    router = APIRouter(prefix='/api/v1/settings')

    def idle():
        if (
            chat.has_active()
            or any(not t.done() for t in chat.tasks.values())
            or experiments.has_active()
        ):
            raise HTTPException(409, '请等待对话和实验完全结束后再修改设置或测试连接。')

    async def parse(request, model=None):
        origin = request.headers.get('origin')
        # Vite forwards the browser Origin while the backend sees the proxy Host.
        # Only the documented local UI ports are trusted, and only on a local backend.
        dev_origins = {
            f'http://{host}:{port}'
            for host in ('localhost', '127.0.0.1', '[::1]')
            for port in (5173, 5174)
        }
        same_origin = origin == str(request.base_url).rstrip('/')
        trusted_dev = origin in dev_origins and loopback_endpoint(str(request.base_url))
        if origin and not (same_origin or trusted_dev):
            raise HTTPException(403, '不允许跨来源修改设置。')
        if (
            request.headers.get('content-type', '').split(';')[0].strip().lower()
            != 'application/json'
        ):
            raise HTTPException(415, '请使用 application/json。')
        try:
            # Bound before parsing: errors must never include request bodies or secret input.
            raw = await request.body()
            if len(raw) > 32768:
                raise ValueError()
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError()
            return model.model_validate(body) if model else body
        except (ValueError, ValidationError):
            raise HTTPException(422, '设置字段无效；请检查地址、必填字段和长度限制。') from None

    def apply(data, *, provider_change):
        values = store.values(data)
        provider = LangChainProvider(values) if provider_change else chat.provider
        store.save(data)
        chat.provider = provider
        experiments.provider = provider
        experiments.settings = values
        chat.system_prompt = experiments.system_prompt = compose_prompt(data['custom_prompt'])

    @router.get('')
    async def get_settings():
        return store.view()

    @router.post('/profiles')
    async def create(request: Request):
        payload = await parse(request, ProfileInput)
        async with chat.lock:
            idle()
            data = deepcopy(store.data)
            if len(data['profiles']) >= 20:
                raise HTTPException(409, '最多保存 20 个连接配置。')
            store.check_key(payload.base_url, payload.api_key)
            data['profiles'].append({'id': str(uuid4()), **payload.model_dump()})
            apply(data, provider_change=False)
            return store.view()

    @router.put('/profiles/{id}')
    async def update(id: str, request: Request):
        payload = await parse(request, ProfileInput)
        async with chat.lock:
            idle()
            data = deepcopy(store.data)
            old = store.profile(id, data)
            if payload.base_url != old['base_url'] and payload.api_key is None:
                raise HTTPException(422, '更改服务地址时必须明确提供新 API key 或清除旧 key。')
            values = payload.model_dump()
            if payload.api_key is None:
                values.pop('api_key')
                values['credential_ref'] = old.get('credential_ref')
            store.check_key(
                values['base_url'],
                values.get('api_key') if 'api_key' in values else values.get('credential_ref'),
            )
            old.clear()
            old.update({'id': id, **values})
            apply(data, provider_change=id == data['active_profile_id'])
            return store.view()

    @router.delete('/profiles/{id}')
    async def delete(id: str, request: Request):
        await parse(request)
        async with chat.lock:
            idle()
            data = deepcopy(store.data)
            profile = store.profile(id, data)
            if id == data['active_profile_id']:
                raise HTTPException(409, '请先切换到其他连接。')
            data['profiles'].remove(profile)
            apply(data, provider_change=False)
            return store.view()

    @router.post('/profiles/{id}/activate')
    async def activate(id: str, request: Request):
        await parse(request)
        async with chat.lock:
            idle()
            data = deepcopy(store.data)
            if id != 'env':
                store.profile(id, data)
            data['active_profile_id'] = id
            apply(data, provider_change=True)
            return store.view()

    @router.put('/prompt')
    async def prompt(request: Request):
        payload = await parse(request, PromptInput)
        async with chat.lock:
            idle()
            data = deepcopy(store.data)
            data['custom_prompt'] = payload.text
            apply(data, provider_change=False)
            return store.view()

    @router.post('/profiles/{id}/test')
    async def test(id: str, request: Request):
        await parse(request)
        async with chat.lock:
            idle()
            store.profile(id)
            values = store.values(id=id)
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

    app.state.user_settings = store
    app.include_router(router)
