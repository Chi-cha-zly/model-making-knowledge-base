import pytest
from pg_test_support import cleanup


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
