"""Explicit real PostgreSQL isolation: each test receives fresh migrated schemas."""

import os
from pathlib import Path
from uuid import uuid4
from sqlalchemy import create_engine, text, event
from sqlalchemy.engine import Engine
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool
from alembic import command
from alembic.config import Config

_urls = {}
_schemas = []
_engines = set()
_connections = set()
_owned_urls = set()


@event.listens_for(Engine, 'engine_connect')
def collect_test_engine(connection):
    if connection.engine.url in _owned_urls:
        _engines.add(connection.engine)
        _connections.add(connection)


def _drop_schema(engine, schema):
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout = '5s'"))
        conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


def test_database_url(key=None):
    key = str(key or 'default')
    if key in _urls:
        return _urls[key]
    value = os.environ.get('PG_TEST_DATABASE_URL')
    if not value:
        raise RuntimeError('Set PG_TEST_DATABASE_URL to a dedicated disposable PostgreSQL database')
    base = make_url(value)
    if base.get_backend_name() != 'postgresql':
        raise RuntimeError('PG_TEST_DATABASE_URL must use PostgreSQL')
    schema = 'test_' + uuid4().hex
    admin = create_engine(base, poolclass=NullPool)
    with admin.begin() as conn:
        conn.execute(text('CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public'))
        conn.execute(text('CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public'))
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    _schemas.append((admin, schema))
    url = base.update_query_dict({'options': f'-csearch_path={schema},public'}).render_as_string(
        hide_password=False
    )
    _owned_urls.add(make_url(url))
    engine = create_engine(url, poolclass=NullPool)
    cfg = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    cfg.set_main_option('script_location', str(Path(__file__).resolve().parents[1] / 'alembic'))
    # public remains visible for extension types/operators, but never provides
    # another catalog's migration version for this schema.
    cfg.attributes['version_table_schema'] = schema
    try:
        with engine.begin() as conn:
            cfg.attributes['connection'] = conn
            command.upgrade(cfg, 'head')
    except BaseException as failure:
        try:
            _drop_schema(admin, schema)
        except Exception:
            # Keep ownership registered for teardown retry without hiding the
            # original migration error behind a cleanup error.
            failure.add_note(f'Cleanup of test schema {schema} failed; teardown will retry')
        else:
            _schemas.remove((admin, schema))
            admin.dispose()
        raise
    finally:
        engine.dispose()
    _urls[key] = url
    return url


test_database_url.__test__ = False


def cleanup():
    # Engine.dispose() alone leaves checked-out connections alive. Close their
    # transactions first so DROP SCHEMA cannot wait on this process's own locks.
    for connection in _connections:
        connection.close()
    _connections.clear()
    for engine in _engines:
        engine.dispose()
    _engines.clear()
    for engine, schema in reversed(_schemas):
        _drop_schema(engine, schema)
        engine.dispose()
        _schemas.remove((engine, schema))
    _schemas.clear()
    _urls.clear()
    _owned_urls.clear()
