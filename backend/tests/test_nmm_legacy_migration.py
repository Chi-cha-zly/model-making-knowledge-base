"""Legacy SQLite imports cannot accept unsupported NMM payloads or ignore target data."""

import sqlite3
import warnings

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import SAWarning
from sqlalchemy.orm import Session

from app.nmm.repository import NmmRepository
from pg_test_support import test_database_url
from scripts.migrate_legacy_sqlite import copy_legacy_database


def legacy_source(tmp_path, *, table='techniques'):
    source = tmp_path / 'legacy.db'
    with sqlite3.connect(source) as connection:
        connection.execute(f'CREATE TABLE {table}(id INTEGER PRIMARY KEY)')
    attachments = tmp_path / 'old-files'
    attachments.mkdir()
    return source, attachments


@pytest.mark.parametrize('apply', [False, True])
def test_legacy_copy_sorts_only_supported_source_tables_without_cycle_warning(tmp_path, apply):
    source, attachments = legacy_source(tmp_path)
    with warnings.catch_warnings():
        warnings.simplefilter('error', SAWarning)
        report = copy_legacy_database(
            source, attachments, test_database_url(), tmp_path / 'new-files', apply=apply
        )
    assert report['status'] == ('completed' if apply else 'dry_run')


@pytest.mark.parametrize('table', ['nmm_projects', 'nmm_assets', 'nmm_plans', 'nmm_steps'])
def test_legacy_copy_rejects_any_nmm_source_table_even_if_empty(tmp_path, table):
    source, attachments = legacy_source(tmp_path, table=table)
    with pytest.raises(ValueError, match='Unsupported source tables: ' + table):
        copy_legacy_database(source, attachments, test_database_url(), tmp_path / 'new-files')


def test_legacy_copy_still_checks_non_source_target_tables_for_rows(tmp_path):
    source, attachments = legacy_source(tmp_path)
    target = test_database_url()
    engine = create_engine(target)
    with Session(engine) as session:
        project = NmmRepository(session).create_project('existing NMM project')
        project_id = project.id
        session.commit()
    with pytest.raises(ValueError, match='Target database must be empty'):
        copy_legacy_database(source, attachments, target, tmp_path / 'new-files', apply=True)
    with Session(engine) as session:
        assert NmmRepository(session).get_project(project_id).title == 'existing NMM project'
    engine.dispose()
    assert not (tmp_path / 'new-files').exists()
