import asyncio
from pathlib import Path
import sqlite3
from typing import TypedDict
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from pg_test_support import test_database_url


class State(TypedDict):
    chosen: str


def graph_for(saver):
    def confirm(state):
        return {'chosen': interrupt({'choices': ['first', 'second']})}

    return (
        StateGraph(State)
        .add_node('confirm', confirm)
        .add_edge(START, 'confirm')
        .add_edge('confirm', END)
        .compile(checkpointer=saver)
    )


def test_checkpoint_tables_migrate_roundtrip_without_runtime_setup():
    engine = create_engine(test_database_url())
    names = {'checkpoints', 'checkpoint_blobs', 'checkpoint_writes', 'checkpoint_migrations'}
    with engine.begin() as connection:
        schema = connection.scalar(text('SELECT current_schema()'))
        assert names <= set(inspect(connection).get_table_names(schema=schema))
        assert connection.scalar(text('SELECT max(v) FROM checkpoint_migrations')) == 9
        cfg = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
        cfg.set_main_option('script_location', str(Path(__file__).resolve().parents[1] / 'alembic'))
        cfg.attributes.update(connection=connection, version_table_schema=schema)
        command.downgrade(cfg, '0013_nmm_foundation')
        assert not names & set(inspect(connection).get_table_names(schema=schema))
        command.upgrade(cfg, 'head')
        assert names <= set(inspect(connection).get_table_names(schema=schema))
    engine.dispose()


def test_checkpoint_resume_in_new_connection_schema_isolation_and_scoped_delete():
    from app.nmm.checkpoints import PostgresCheckpointStore, delete_project_checkpoints

    engine = create_engine(test_database_url('first'))
    other = create_engine(test_database_url('second'))
    project = uuid4()
    thread = f'nmm:{project}:1:{uuid4()}'
    unrelated = f'nmm:{uuid4()}:1:{uuid4()}'
    config = {'configurable': {'thread_id': thread}}

    async def run():
        async with PostgresCheckpointStore(engine).open() as saver:
            result = await graph_for(saver).ainvoke({'chosen': ''}, config)
            assert result['__interrupt__'][0].value == {'choices': ['first', 'second']}
            await graph_for(saver).ainvoke(
                {'chosen': ''}, {'configurable': {'thread_id': unrelated}}
            )
        async with PostgresCheckpointStore(other).open() as saver:
            assert await saver.aget_tuple(config) is None
        async with PostgresCheckpointStore(engine).open() as saver:
            result = await graph_for(saver).ainvoke(Command(resume='second'), config)
            assert result['chosen'] == 'second' and not result.get('__interrupt__')
        with Session(engine) as session:
            delete_project_checkpoints(session, project)
            session.commit()
        async with PostgresCheckpointStore(engine).open() as saver:
            assert await saver.aget_tuple(config) is None
            assert await saver.aget_tuple({'configurable': {'thread_id': unrelated}}) is not None

    asyncio.run(run())
    engine.dispose()
    other.dispose()


@pytest.mark.parametrize(
    'table', ['checkpoints', 'checkpoint_blobs', 'checkpoint_writes', 'checkpoint_migrations']
)
def test_legacy_import_rejects_checkpoint_source_tables(tmp_path, table):
    from scripts.migrate_legacy_sqlite import copy_legacy_database

    source = tmp_path / 'legacy.db'
    with sqlite3.connect(source) as connection:
        connection.execute(f'CREATE TABLE {table}(id INTEGER PRIMARY KEY)')
    files = tmp_path / 'inputs'
    files.mkdir()
    with pytest.raises(ValueError, match='Unsupported source tables: ' + table):
        copy_legacy_database(source, files, test_database_url(), tmp_path / 'outputs')


def test_legacy_import_checks_checkpoint_data_but_not_migration_marker(tmp_path):
    from scripts.migrate_legacy_sqlite import copy_legacy_database

    source = tmp_path / 'legacy.db'
    with sqlite3.connect(source) as connection:
        connection.execute('CREATE TABLE techniques(id INTEGER PRIMARY KEY)')
    files = tmp_path / 'inputs'
    files.mkdir()
    target = test_database_url()
    assert copy_legacy_database(source, files, target, tmp_path / 'outputs')['status'] == 'dry_run'
    engine = create_engine(target)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO checkpoints(thread_id, checkpoint_id, checkpoint) VALUES ('existing', 'one', '{}')"
            )
        )
    with pytest.raises(ValueError, match='Target database must be empty'):
        copy_legacy_database(source, files, target, tmp_path / 'outputs')
    engine.dispose()
