"""Atomic settings manifests and OS credential lifecycle; no HTTP transport."""

from copy import deepcopy
import json
import hashlib
import os
from pathlib import Path
import tempfile
from uuid import uuid4
from ..prompts import SYSTEM, PRESETS
from ..settings_schemas import ProfileInput, PromptInput
from ..runtime_config import valid_endpoint, loopback_endpoint
from ..services.errors import ServiceError


class PublishedWriteError(OSError):
    """Rename succeeded, but its durability could not be confirmed."""

    def __init__(self, serialized):
        super().__init__('Settings published; directory durability confirmation pending')
        self.serialized = serialized


class PrivateSettings:
    def __init__(self, path, env, *, credentials=None):
        from filelock import FileLock
        from ..credentials import SystemCredentials

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
            raise ServiceError(422, '非本机服务需要 API key。')

    def profile(self, id, data=None):
        if id == 'env':
            raise ServiceError(409, '环境配置为只读。')
        for profile in (self.data if data is None else data)['profiles']:
            if profile['id'] == id:
                return profile
        raise ServiceError(404, '未找到连接配置。')

    def _credential(self, method, *args):
        from ..credentials import CredentialError

        try:
            return getattr(self.credentials, method)(*args)
        except Exception:
            raise CredentialError(
                '系统凭据操作失败；请检查凭据库状态。不会退回明文或环境密钥。'
            ) from None

    def key(self, profile):
        from ..credentials import CredentialError

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
        from ..credentials import CredentialError

        current = self.path.read_bytes() if self.path.exists() else None
        if current != self._file_bytes:
            raise CredentialError('私有设置已被另一个进程修改；请重新启动后再试。')

    def _references(self, data):
        return {p['credential_ref'] for p in data['profiles'] if p.get('credential_ref')}

    def _recover(self):
        from ..credentials import CredentialError

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
        from ..credentials import CredentialError

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
