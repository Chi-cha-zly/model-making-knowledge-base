from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from pg_test_support import test_database_url
from app.nmm.models import NmmAsset, NmmPlan, NmmProject, NmmStep
from app.nmm.repository import NmmRepository
from app.nmm.schemas import NmmParams


@pytest.fixture
def db():
    engine = create_engine(test_database_url())
    with Session(engine) as session:
        yield session
    engine.dispose()


def asset(repo, project, role='candidate', **kwargs):
    return repo.add_asset(
        project.id,
        role=role,
        storage_name=f'{uuid4()}.png',
        content_type='image/png',
        size=20,
        width=4,
        height=4,
        sha256='a' * 64,
        **kwargs,
    )


def test_repository_refs_and_delete_cycle(db):
    repo = NmmRepository(db)
    project = repo.create_project('test')
    other = repo.create_project('other')
    candidate = asset(repo, project)
    foreign = asset(repo, other)
    original = asset(repo, project, 'original')
    for invalid in (foreign, original):
        with pytest.raises(ValueError):
            repo.select_candidate(project.id, invalid.id)
        with pytest.raises(ValueError):
            repo.create_plan(project.id, invalid.id, plan={}, palette=[], renderer_version='v1')
    repo.select_candidate(project.id, candidate.id)
    plan = repo.create_plan(project.id, candidate.id, plan={}, palette=[], renderer_version='v1')
    image = asset(repo, project, 'step')
    diff = asset(repo, project, 'step_diff')
    with pytest.raises(ValueError):
        repo.add_step(plan.id, index=1, title='bad', image_asset_id=original.id)
    with pytest.raises(ValueError):
        repo.add_step(
            plan.id, index=1, title='bad', image_asset_id=image.id, diff_asset_id=candidate.id
        )
    repo.add_step(
        plan.id,
        index=1,
        title='paint',
        image_asset_id=image.id,
        diff_asset_id=diff.id,
        description={'text': 'paint'},
    )
    db.commit()
    repo.delete_project(project.id)
    db.commit()
    assert db.get(NmmProject, project.id) is None
    assert list(db.scalars(select(NmmAsset).where(NmmAsset.project_id == project.id))) == []
    assert list(db.scalars(select(NmmPlan))) == []
    assert list(db.scalars(select(NmmStep))) == []
    assert db.get(NmmProject, other.id) is not None


def test_revision_validation_and_selected_asset_cleanup(db):
    repo = NmmRepository(db)
    project = repo.create_project('revision')
    candidate = asset(repo, project)
    repo.select_candidate(project.id, candidate.id)
    repo.delete_asset(candidate.id)
    assert project.selected_candidate_id is None
    candidate = asset(repo, project)
    plan = repo.create_plan(project.id, candidate.id, plan={}, palette=[], renderer_version='v1')
    project.revision = 2
    db.flush()
    with pytest.raises(ValueError):
        repo.select_candidate(project.id, candidate.id)
    with pytest.raises(ValueError):
        asset(repo, project, revision=1)
    with pytest.raises(ValueError):
        repo.add_step(plan.id, index=1, title='stale', image_asset_id=candidate.id)
    with pytest.raises(ValueError):
        repo.add_asset(project.id, role='original', storage_name='../escape.png')


@pytest.mark.parametrize(
    'change', [{'role': 'invalid'}, {'revision': 0}, {'width': 0}, {'project_id': uuid4()}]
)
def test_database_constraints(db, change):
    repo = NmmRepository(db)
    project = repo.create_project('constraints')
    row = asset(repo, project)
    with pytest.raises(IntegrityError), db.begin_nested():
        for name, value in change.items():
            setattr(row, name, value)
        db.flush()


def test_database_step_fk_and_unique_index(db):
    repo = NmmRepository(db)
    project = repo.create_project('step')
    candidate = asset(repo, project)
    plan = repo.create_plan(project.id, candidate.id, plan={}, palette=[], renderer_version='v1')
    image = asset(repo, project, 'step')
    repo.add_step(plan.id, index=1, title='one', image_asset_id=image.id)
    with pytest.raises(IntegrityError), db.begin_nested():
        repo.add_step(plan.id, index=1, title='duplicate', image_asset_id=image.id)
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(NmmStep(plan_id=plan.id, index=2, title='missing', image_asset_id=uuid4()))
        db.flush()


def test_migration_roundtrip_and_schema_isolation(db):
    connection = db.connection()
    schema = connection.scalar(text('select current_schema()'))
    cfg = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    cfg.set_main_option('script_location', str(Path(__file__).resolve().parents[1] / 'alembic'))
    cfg.attributes.update(connection=connection, version_table_schema=schema)
    command.downgrade(cfg, '0012_agent_traces')
    assert 'nmm_projects' not in inspect(connection).get_table_names(schema=schema)
    command.upgrade(cfg, 'head')
    assert {'nmm_projects', 'nmm_assets', 'nmm_plans', 'nmm_steps'} <= set(
        inspect(connection).get_table_names(schema=schema)
    )
    other_engine = create_engine(test_database_url('other'))
    with other_engine.connect() as other:
        assert other.scalar(text('select current_schema()')) != schema
        assert other.scalar(text('select count(*) from nmm_projects')) == 0
    other_engine.dispose()


def test_params_bounds_and_json_copy(db):
    for count in (3, 10):
        with pytest.raises(ValidationError):
            NmmParams(step_count=count)
    repo = NmmRepository(db)
    project = repo.create_project('json')
    candidate = asset(repo, project)
    value = {'layers': []}
    plan = repo.create_plan(project.id, candidate.id, plan=value, palette=[], renderer_version='v1')
    value['layers'].append('external mutation')
    assert plan.plan == {'layers': []}
    db.rollback()
    assert db.get(NmmProject, project.id) is None


def test_single_input_roles_and_field_protection(db):
    repo = NmmRepository(db)
    project = repo.create_project('inputs')
    asset(repo, project, 'original')
    with pytest.raises(IntegrityError), db.begin_nested():
        asset(repo, project, 'original')
    with pytest.raises(ValueError):
        asset(repo, project, id=uuid4())
    asset(repo, project, 'reference_steps')
    asset(repo, project, 'reference_steps')


def test_job_set_null_and_referenced_asset_restrict(db):
    from app.job_models import Job

    repo = NmmRepository(db)
    project = repo.create_project('job')
    job = Job(kind='nmm', request_id='test', request_hash='a' * 64)
    db.add(job)
    db.flush()
    project.active_job_id = job.id
    db.flush()
    db.delete(job)
    db.flush()
    db.refresh(project)
    assert project.active_job_id is None
    candidate = asset(repo, project)
    repo.create_plan(project.id, candidate.id, plan={}, palette=[], renderer_version='v1')
    with pytest.raises(IntegrityError), db.begin_nested():
        repo.delete_asset(candidate.id)


def test_status_constraint_and_indexes(db):
    repo = NmmRepository(db)
    project = repo.create_project('status')
    with pytest.raises(IntegrityError), db.begin_nested():
        project.status = 'invented'
        db.flush()
    schema = db.scalar(text('select current_schema()'))
    indexes = inspect(db.connection()).get_indexes('nmm_assets', schema=schema)
    assert any(item['name'] == 'uq_nmm_assets_input_role' and item['unique'] for item in indexes)
    assert any(
        item['column_names'] == ['project_id', 'revision', 'role', 'sort_order'] for item in indexes
    )
