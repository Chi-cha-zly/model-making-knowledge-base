"""Filesystem compensation and input relationships at the NMM service boundary."""

from io import BytesIO

import pytest
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.nmm.models import NmmAsset, NmmProject
from app.nmm.repository import NmmRepository
from app.nmm.service import NmmService
from pg_test_support import test_database_url


def image(color, size=(4, 3)):
    from app.nmm.providers import ImageInput

    output = BytesIO()
    Image.new('RGB', size, color).save(output, 'PNG')
    return ImageInput(output.getvalue(), 'image/png')


@pytest.fixture
def engine():
    value = create_engine(test_database_url())
    yield value
    value.dispose()


def test_create_preserves_input_relationships(engine, tmp_path):
    service = NmmService(engine, tmp_path / 'attachments')
    project_id = service.create_project(
        title='肩甲',
        params={'metal': 'gold'},
        original=image('grey'),
        mask=image('white'),
        references=[('reference_steps', image('blue'))],
    )
    with Session(engine) as db:
        project = db.get(NmmProject, project_id)
        assets = list(db.scalars(select(NmmAsset).where(NmmAsset.project_id == project_id)))
        assert project.status == 'draft'
        assert project.params['step_count'] == 7
        assert project.input_snapshots['1']['params'] == project.params
        assert {row.role for row in assets} == {'original', 'mask', 'reference_steps'}
        for row in assets:
            raw, content_type = service.read_asset(row.id)
            assert content_type == 'image/png'
            assert Image.open(BytesIO(raw)).size == (4, 3)
            assert (tmp_path / 'nmm' / str(project_id) / row.storage_name).is_file()
    assert not (tmp_path / 'attachments').exists()


def test_invalid_mask_creates_no_project_or_files(engine, tmp_path):
    service = NmmService(engine, tmp_path / 'attachments')
    with pytest.raises(ValueError):
        service.create_project(title='bad', original=image('grey'), mask=image('white', (2, 2)))
    with Session(engine) as db:
        assert list(db.scalars(select(NmmProject))) == []
    assert list(tmp_path.rglob('*.png')) == []


def test_database_failure_rolls_back_files(engine, tmp_path, monkeypatch):
    service = NmmService(engine, tmp_path / 'attachments')
    original = NmmRepository.add_asset
    calls = 0

    def fail_second(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError('injected database failure')
        return original(self, *args, **kwargs)

    monkeypatch.setattr(NmmRepository, 'add_asset', fail_second)
    with pytest.raises(RuntimeError, match='injected'):
        service.create_project(title='rollback', original=image('grey'), mask=image('white'))
    with Session(engine) as db:
        assert list(db.scalars(select(NmmProject))) == []
        assert list(db.scalars(select(NmmAsset))) == []
    assert list(tmp_path.rglob('*.png')) == []


def test_reference_role_validation_precedes_writes(engine, tmp_path):
    service = NmmService(engine, tmp_path / 'attachments')
    with pytest.raises(ValueError):
        service.create_project(
            title='bad role',
            original=image('grey'),
            mask=image('white'),
            references=[('candidate', image('red'))],
        )
    assert list(tmp_path.rglob('*.png')) == []
