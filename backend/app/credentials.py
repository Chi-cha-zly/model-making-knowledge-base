"""Lazy system-vault boundary. Never falls back to plaintext or a null backend."""
from typing import Protocol

SERVICE = 'model-making-knowledge-base.credentials'


class CredentialError(RuntimeError):
    """Safe to return to the client; never includes backend exception text."""


class CredentialStore(Protocol):
    def get(self, reference: str) -> str | None: ...
    def set(self, reference: str, secret: str) -> None: ...
    def delete(self, reference: str) -> None: ...


class SystemCredentials:
    def __init__(self):
        self._backend = None

    def _native(self):
        if self._backend is None:
            try:
                import keyring
                backend = keyring.get_keyring()
                kind = (type(backend).__module__, type(backend).__name__)
                supported = {
                    ('keyring.backends.macOS', 'Keyring'),
                    ('keyring.backends.Windows', 'WinVaultKeyring'),
                    ('keyring.backends.SecretService', 'Keyring'),
                }
                if kind not in supported or backend.priority <= 0:
                    raise ValueError()
                self._backend = backend
            except Exception:
                raise CredentialError('系统凭据库不可用；请启用原生 Keychain、Credential Locker 或 Secret Service。不会改用明文保存。') from None
        return self._backend

    def get(self, reference):
        try:
            return self._native().get_password(SERVICE, reference)
        except CredentialError:
            raise
        except Exception:
            raise CredentialError('无法读取系统凭据；请检查凭据库是否已解锁。') from None

    def set(self, reference, secret):
        try:
            self._native().set_password(SERVICE, reference, secret)
        except CredentialError:
            raise
        except Exception:
            raise CredentialError('无法保存系统凭据；原有设置保持可恢复。') from None

    def delete(self, reference):
        try:
            backend = self._native()
            if backend.get_password(SERVICE, reference) is not None:
                backend.delete_password(SERVICE, reference)
        except CredentialError:
            raise
        except Exception:
            raise CredentialError('旧系统凭据清理尚未完成，可在下次启动重试。') from None
