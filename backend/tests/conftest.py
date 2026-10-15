"""Isolate operator configuration before collection imports application modules."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from pg_test_support import cleanup
import app.runtime_config as runtime_config

# Installed at conftest import time, before app.llm/runtime bind read_settings.
# Explicit fixture files/mappings are still parsed by the production parser.
_original_read_settings = runtime_config.read_settings
_test_storage = TemporaryDirectory(prefix='modelkb-pytest-')
_empty_settings = Path(_test_storage.name) / 'absent.env'


def isolated_settings(path=None, environ=None):
    if path is not None or environ is not None:
        return _original_read_settings(
            path if path is not None else _empty_settings,
            {} if environ is None else environ,
        )
    return {
        'DATA_DIR': str(Path(_test_storage.name) / 'attachments'),
        # Database tests must supply their fixture schema URL explicitly.
        'DATABASE_URL': 'postgresql+psycopg://blocked:blocked@127.0.0.1:1/blocked',
        **(
            {'BACKUP_MAX_BYTES': os.environ['BACKUP_MAX_BYTES']}
            if 'BACKUP_MAX_BYTES' in os.environ
            else {}
        ),
    }


runtime_config.read_settings = isolated_settings


@pytest.fixture(autouse=True)
def isolated_postgres_schemas():
    yield
    cleanup()


@pytest.fixture(autouse=True)
def fake_system_credentials(monkeypatch):
    # Every automated test stays off the real OS vault, including app restarts.
    from test_credentials import FakeCredentials

    vault = FakeCredentials()
    monkeypatch.setattr('app.credentials.SystemCredentials', lambda: vault)
    return vault
