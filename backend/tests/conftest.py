import pytest
from pg_test_support import cleanup


@pytest.fixture(autouse=True)
def isolated_postgres_schemas():
    yield
    cleanup()
