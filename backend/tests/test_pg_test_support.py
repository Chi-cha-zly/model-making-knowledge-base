"""Isolation remains local even when public already contains a migrated catalog."""

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import NullPool

import pg_test_support as support
from app.models import Base


def migration_config():
    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / 'alembic.ini'))
    cfg.set_main_option('script_location', str(root / 'alembic'))
    return cfg


def public_snapshot(connection):
    relations = connection.execute(
        text("""
        SELECT c.relname, c.relkind, c.oid FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'S', 'i')
        ORDER BY c.relname
    """)
    ).all()
    rows = {}
    for name, kind, _ in relations:
        if kind in ('r', 'S'):
            values = connection.execute(
                text(f'SELECT to_jsonb(t) FROM (SELECT * FROM public."{name}") t')
            ).scalars()
            rows[name] = sorted(json.dumps(value, sort_keys=True) for value in values)
    return hashlib.sha256(
        json.dumps([list(map(tuple, relations)), rows], sort_keys=True).encode()
    ).hexdigest()


@pytest.fixture
def migrated_public():
    """Fixture owns only newly created public tables and its explicit sentinel row."""
    value = os.environ['PG_TEST_DATABASE_URL']
    engine = create_engine(value, poolclass=NullPool)
    cfg = migration_config()
    head = ScriptDirectory.from_config(cfg).get_current_head()
    created = []
    sentinel_id = -int(uuid4().hex[:7], 16) - 1
    with engine.begin() as connection:
        existing = inspect(connection).get_table_names(schema='public')
        if 'alembic_version' not in existing:
            assert not existing, (
                'Isolation regression requires empty or fully migrated public schema'
            )
            connection.execute(text('SET LOCAL search_path = public'))
            cfg.attributes['connection'] = connection
            cfg.attributes['version_table_schema'] = 'public'
            command.upgrade(cfg, 'head')
            created = inspect(connection).get_table_names(schema='public')
        assert connection.scalar(text('SELECT version_num FROM public.alembic_version')) == head
        connection.execute(
            text("""
            INSERT INTO public.sources
            (id,title,source_type,retrieved_at,owner_id,visibility,status)
            VALUES (:id,'public isolation sentinel','local',now(),'local','private','published')
        """),
            {'id': sentinel_id},
        )
    try:
        yield engine
    finally:
        # First dispose/drop only schemas and connections registered by this process.
        support.cleanup()
        with engine.begin() as connection:
            if created:
                for name in created:
                    connection.execute(text(f'DROP TABLE IF EXISTS public."{name}" CASCADE'))
            else:
                connection.execute(
                    text('DELETE FROM public.sources WHERE id=:id'), {'id': sentinel_id}
                )
        engine.dispose()


def test_migrated_public_is_not_reused_or_changed(migrated_public):
    admin = migrated_public
    with admin.connect() as connection:
        before = public_snapshot(connection)
        existing_schemas = set(
            connection.execute(text('SELECT nspname FROM pg_namespace')).scalars()
        )
    first_url = support.test_database_url('first isolated catalog')
    second_url = support.test_database_url('second isolated catalog')
    assert first_url != second_url
    assert support.test_database_url('first isolated catalog') == first_url
    engines = [create_engine(url) for url in (first_url, second_url)]
    schemas = []
    for index, engine in enumerate(engines):
        with engine.begin() as connection:
            schema = connection.scalar(text('SELECT current_schema()'))
            schemas.append(schema)
            tables = set(inspect(connection).get_table_names(schema=schema))
            assert tables == set(Base.metadata.tables) | {'alembic_version'}
            assert connection.scalar(
                text('SELECT version_num FROM alembic_version')
            ) == connection.scalar(text('SELECT version_num FROM public.alembic_version'))
            # Version/table/index resolution must point into this test's schema.
            for name in tables:
                assert (
                    connection.scalar(
                        text(
                            'SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.oid=to_regclass(:name)'
                        ),
                        {'name': name},
                    )
                    == schema
                )
            index_names = (
                connection.execute(
                    text('SELECT indexname FROM pg_indexes WHERE schemaname=:schema'),
                    {'schema': schema},
                )
                .scalars()
                .all()
            )
            assert set(index_names) == set(
                connection.execute(
                    text("SELECT indexname FROM pg_indexes WHERE schemaname='public'")
                ).scalars()
            )
            foreign_schemas = (
                connection.execute(
                    text("""
                SELECT DISTINCT rn.nspname FROM pg_constraint fk
                JOIN pg_class owner ON owner.oid=fk.conrelid
                JOIN pg_namespace ownn ON ownn.oid=owner.relnamespace
                JOIN pg_class referenced ON referenced.oid=fk.confrelid
                JOIN pg_namespace rn ON rn.oid=referenced.relnamespace
                WHERE fk.contype='f' AND ownn.nspname=:schema
            """),
                    {'schema': schema},
                )
                .scalars()
                .all()
            )
            assert foreign_schemas == [schema]
            assert connection.scalar(text('SELECT count(*) FROM sources')) == 0
            connection.execute(
                text(
                    "INSERT INTO sources (title,source_type,retrieved_at,owner_id,visibility,status) VALUES (:title,'local',now(),'local','private','published')"
                ),
                {'title': f'private {index}'},
            )
    for index, engine in enumerate(engines):
        with engine.connect() as connection:
            assert connection.execute(text('SELECT title FROM sources')).scalars().all() == [
                f'private {index}'
            ]
    support.cleanup()
    with admin.connect() as connection:
        assert public_snapshot(connection) == before
        after_schemas = set(connection.execute(text('SELECT nspname FROM pg_namespace')).scalars())
        assert after_schemas == existing_schemas
        assert not set(schemas) & after_schemas


def test_failed_migration_drops_only_its_schema_and_can_retry(monkeypatch):
    admin = create_engine(os.environ['PG_TEST_DATABASE_URL'], poolclass=NullPool)
    with admin.connect() as connection:
        before = set(connection.execute(text('SELECT nspname FROM pg_namespace')).scalars())
    upgrade = support.command.upgrade

    def fail(cfg, revision):
        cfg.attributes['connection'].execute(text('CREATE TABLE partially_migrated(id integer)'))
        raise RuntimeError('synthetic migration failure')

    with monkeypatch.context() as patch:
        patch.setattr(support.command, 'upgrade', fail)
        with pytest.raises(RuntimeError, match='synthetic migration failure'):
            support.test_database_url('retry after failed migration')
    with admin.connect() as connection:
        assert set(connection.execute(text('SELECT nspname FROM pg_namespace')).scalars()) == before
    assert support.command.upgrade is upgrade
    url = support.test_database_url('retry after failed migration')
    with create_engine(url).connect() as connection:
        assert connection.scalar(text('SELECT count(*) FROM sources')) == 0
    admin.dispose()


def test_cleanup_closes_checked_out_connections_before_dropping_schema():
    engine = create_engine(support.test_database_url('connection cleanup'))
    connection = engine.connect()
    connection.execute(text('SELECT count(*) FROM sources'))
    support.cleanup()
    assert connection.closed
    assert engine.pool.checkedout() == 0


def test_external_lock_cleanup_times_out_and_can_retry():
    from sqlalchemy.exc import OperationalError

    url = support.test_database_url('external lock')
    with create_engine(url).connect() as connection:
        schema = connection.scalar(text('SELECT current_schema()'))
    # This admin connection is deliberately outside the helper's ownership.
    external_engine = create_engine(os.environ['PG_TEST_DATABASE_URL'], poolclass=NullPool)
    external = external_engine.connect()
    try:
        external.execute(text(f'SELECT count(*) FROM "{schema}".sources'))
        with pytest.raises(OperationalError, match='lock timeout'):
            support.cleanup()
        assert not external.closed
        assert (
            external.scalar(
                text('SELECT count(*) FROM pg_namespace WHERE nspname=:schema'), {'schema': schema}
            )
            == 1
        )
    finally:
        external.close()
        external_engine.dispose()
    support.cleanup()
    with create_engine(
        os.environ['PG_TEST_DATABASE_URL'], poolclass=NullPool
    ).connect() as connection:
        assert (
            connection.scalar(
                text('SELECT count(*) FROM pg_namespace WHERE nspname=:schema'), {'schema': schema}
            )
            == 0
        )
