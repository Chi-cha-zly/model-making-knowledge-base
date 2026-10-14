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


@event.listens_for(Engine, "engine_connect")
def collect_test_engine(connection):
    if "search_path=test_" in str(connection.engine.url.query.get("options", "")):
        _engines.add(connection.engine)


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
    url = base.update_query_dict({'options': f'-csearch_path={schema},public'}).render_as_string(hide_password=False)
    engine = create_engine(url, poolclass=NullPool)
    cfg = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    cfg.set_main_option('script_location', str(Path(__file__).resolve().parents[1] / 'alembic'))
    with engine.begin() as conn:
        cfg.attributes['connection'] = conn
        command.upgrade(cfg, 'head')
    engine.dispose()
    _urls[key] = url
    return url


test_database_url.__test__ = False


def cleanup():
    for engine in _engines:
        engine.dispose()
    _engines.clear()
    for engine, schema in reversed(_schemas):
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()
    _schemas.clear()
    _urls.clear()
