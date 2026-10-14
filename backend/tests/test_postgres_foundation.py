import pytest
from pydantic import ValidationError
from app.schemas import TechniqueInput, TechniquePatch


def test_modern_techniques_reject_legacy_steps():
    with pytest.raises(ValidationError):
        TechniqueInput(title='legacy', original=True, steps=['must migrate'])
    with pytest.raises(ValidationError):
        TechniquePatch(steps=['must migrate'])


def test_runtime_rejects_sqlite_before_connecting():
    from app.db import create_db_engine
    with pytest.raises(ValueError, match='PostgreSQL'):
        create_db_engine('sqlite://')


def test_migration_preserves_legacy_steps_and_existing_blocks():
    from pg_test_support import test_database_url
    from sqlalchemy import create_engine, text, inspect
    from alembic import command
    from alembic.config import Config
    from pathlib import Path
    engine = create_engine(test_database_url())
    config = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    config.set_main_option('script_location', str(Path(__file__).resolve().parents[1] / 'alembic'))
    with engine.begin() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, '0007_ingestion')
        connection.execute(text("""INSERT INTO techniques
            (id,title,steps,content_blocks,tags,owner_id,visibility,status) VALUES
            (1,'old','["先打磨", {"确认": false,"数量": 0}]','[]','[]','local','private','published'),
            (2,'new','["duplicated"]','[{"type":"paragraph","text":"正文"}]','[]','local','private','published')"""))
        command.upgrade(config, 'head')
        assert 'steps' not in {c['name'] for c in inspect(connection).get_columns('techniques')}
        blocks = dict(connection.execute(text('SELECT id, content_blocks FROM techniques')).all())
        assert blocks[1] == [{'type':'paragraph','text':'先打磨'}, {'type':'paragraph','text':'确认：否\n数量：0'}]
        assert blocks[2] == [{'type':'paragraph','text':'正文'}]
        assert connection.scalar(text("SELECT typname FROM pg_type WHERE oid=(SELECT atttypid FROM pg_attribute WHERE attrelid='embeddings'::regclass AND attname='vector')")) == 'vector'
    engine.dispose()


def test_explicit_legacy_copy_is_dry_run_and_preserves_source(tmp_path):
    from scripts.migrate_legacy_sqlite import copy_legacy_database
    from pg_test_support import test_database_url
    import sqlite3
    import json
    from sqlalchemy import create_engine, text
    source = tmp_path / 'old.db'
    with sqlite3.connect(source) as conn:
        conn.execute('CREATE TABLE techniques(id INTEGER PRIMARY KEY, title TEXT, steps JSON, content_blocks JSON, tags JSON, owner_id TEXT, visibility TEXT, status TEXT)')
        conn.execute('INSERT INTO techniques VALUES(1,?,?,?,?,?,?,?)', ('旧文', json.dumps(['先剪后磨']), '[]', '[]', 'local', 'private', 'published'))
    source_bytes = source.read_bytes()
    attachments = tmp_path / 'old-files'; attachments.mkdir()
    target = test_database_url()
    output = tmp_path / 'new-files'
    plan = copy_legacy_database(source, attachments, target, output)
    assert plan['status'] == 'dry_run' and not output.exists()
    done = copy_legacy_database(source, attachments, target, output, apply=True)
    assert done['status'] == 'completed' and done['tables']['techniques'] == 1
    with create_engine(target).connect() as conn:
        assert conn.scalar(text('SELECT content_blocks FROM techniques')) == [{'type':'paragraph','text':'先剪后磨'}]
    assert source.read_bytes() == source_bytes
    with pytest.raises(ValueError, match='empty'):
        copy_legacy_database(source, attachments, target, output, apply=True)


def test_legacy_conversion_preserves_long_and_many_steps_within_modern_bounds():
    from app.content import legacy_steps_to_blocks, content_adapter
    for steps in (['字' * 25000], [f'第{i}步' for i in range(201)]):
        blocks = legacy_steps_to_blocks(steps)
        content_adapter.validate_python(blocks)
        assert ''.join(block['text'] for block in blocks).replace('\n','') == ''.join(steps)
    with pytest.raises(ValueError, match='100000'):
        legacy_steps_to_blocks(['字'*100001])


def test_alembic_refuses_sqlite_before_creating_any_tables(tmp_path):
    from sqlalchemy import create_engine, inspect
    from alembic import command
    from alembic.config import Config
    from pathlib import Path
    engine = create_engine('sqlite:///' + str(tmp_path / 'untouched.db'))
    cfg = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    cfg.set_main_option('script_location', str(Path(__file__).resolve().parents[1] / 'alembic'))
    with engine.begin() as connection:
        cfg.attributes['connection'] = connection
        with pytest.raises(ValueError, match='PostgreSQL'):
            command.upgrade(cfg, 'head')
        assert inspect(connection).get_table_names() == []
    engine.dispose()


def test_legacy_copy_rejects_file_changed_during_staging(tmp_path, monkeypatch):
    import sqlite3
    import hashlib
    from scripts import migrate_legacy_sqlite as migration
    from pg_test_support import test_database_url
    from sqlalchemy import create_engine, text
    source = tmp_path / 'old.db'
    files = tmp_path / 'old-files'; files.mkdir()
    raw = b'original image'
    (files / 'old.png').write_bytes(raw)
    with sqlite3.connect(source) as conn:
        conn.execute('CREATE TABLE attachments(id INTEGER PRIMARY KEY, storage_name TEXT, size INTEGER, sha256 TEXT)')
        conn.execute('INSERT INTO attachments VALUES(1,?,?,?)', ('old.png',len(raw),hashlib.sha256(raw).hexdigest()))
    original = migration.shutil.copy2
    def corrupt_staged(src, dst):
        original(src,dst)
        dst.write_bytes(b'changed after original validation')
    monkeypatch.setattr(migration.shutil,'copy2',corrupt_staged)
    target = test_database_url()
    with pytest.raises(ValueError, match='changed during copy'):
        migration.copy_legacy_database(source,files,target,tmp_path/'new-files',apply=True)
    with create_engine(target).connect() as connection:
        assert connection.scalar(text('SELECT COUNT(*) FROM attachments')) == 0
    assert not (tmp_path/'new-files').exists()
    assert (files/'old.png').read_bytes() == raw
