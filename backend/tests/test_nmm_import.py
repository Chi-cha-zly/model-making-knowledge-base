from pg_test_support import test_database_url
"""Importer regressions: selection, atomic writes, retry and original evidence."""
import base64
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session

from app.models import Base, Source, Technique, Attachment

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')


def test_dry_run_requires_explicit_markdown_and_never_creates_target(tmp_path):
    from scripts.import_nmm import import_directory
    source = tmp_path / 'input'
    source.mkdir()
    (source / '图片.png').write_bytes(PNG)
    (source / '知识.md').write_text('# 非金属金属\n薄涂。')
    (source / 'nmm需求文档.md').write_text('开发提示词不是知识')
    target = tmp_path / 'target'
    report = import_directory(source, test_database_url(f'{target}/kb.db'), target / 'attachments',
                              markdown=['知识.md'], dry_run=True)
    assert report['counts']['planned'] == 2
    assert report['counts']['skipped'] == 1
    assert next(x for x in report['files'] if x['filename'] == 'nmm需求文档.md')['reason'] == 'markdown_not_selected'
    assert not target.exists()


@pytest.fixture
def import_target(tmp_path):
    # Production ingest tables must exist before importing.
    import app.ingestion_models  # noqa: F401
    engine = create_engine(test_database_url(f'{tmp_path}/kb.db'))
    source = tmp_path / 'input'
    source.mkdir()
    return source, engine, tmp_path / 'attachments'


def test_import_preserves_ocr_image_source_and_is_hash_idempotent(import_target):
    from scripts.import_nmm import import_directory
    source, engine, storage = import_target
    (source / '银剑_1_作者_来自小红书网页版.png').write_bytes(PNG)
    (source / '知识.md').write_text('# 非金属金属\n薄涂。')
    ocr_text = '底色 AK11190\n' + '高光需要保留暗面。' * 180
    result = import_directory(source, engine.url.render_as_string(hide_password=False), storage, markdown=['知识.md'],
                              ocr=lambda path: ocr_text)
    assert result['counts']['imported'] == 2
    with Session(engine) as db:
        rows = db.scalars(select(Technique)).all()
        assert len(rows) == 2
        image = next(x for x in rows if x.attachments)
        assert image.sources[0].author == '作者'
        blocks = image.content_blocks
        assert all(b['attachment_id'] == image.attachments[0].id for b in blocks if b['type'] == 'image')
        assert ocr_text in image.summary
        assert ''.join(b['caption'] for b in blocks if b['type'] == 'image').endswith(ocr_text)
        assert (storage / image.attachments[0].storage_name).read_bytes() == PNG
    # A renamed byte-identical image is still the same source.
    next(source.glob('*.png')).rename(source / '改名.png')
    again = import_directory(source, engine.url.render_as_string(hide_password=False), storage, markdown=['知识.md'],
                             ocr=lambda path: pytest.fail('duplicate OCR should be skipped'))
    assert again['counts']['unchanged'] == 2
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Source)) == 2
        assert db.scalar(select(func.count()).select_from(Attachment)) == 1


def test_ocr_failure_has_no_partial_database_or_attachment_writes(import_target):
    from scripts.import_nmm import import_directory
    source, engine, storage = import_target
    (source / '坏.png').write_bytes(PNG)
    def unavailable(path):
        raise RuntimeError('OCR unavailable')
    result = import_directory(source, engine.url.render_as_string(hide_password=False), storage, ocr=unavailable)
    assert result['counts']['failed'] == 1
    assert result['files'][0]['ocr_status'] == 'failed'
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Source)) == 0
        assert db.scalar(select(func.count()).select_from(Attachment)) == 0
    assert not list(storage.glob('*'))


def test_ocr_empty_is_explicit_and_not_invented(import_target):
    from scripts.import_nmm import import_directory
    source, engine, storage = import_target
    (source / '纯图.png').write_bytes(PNG)
    result = import_directory(source, engine.url.render_as_string(hide_password=False), storage, ocr=lambda path: '')
    assert result['counts']['imported'] == 1
    assert result['files'][0]['ocr_status'] == 'empty'
    with Session(engine) as db:
        row = db.scalar(select(Technique))
        assert '未提取到可识别文字' in row.summary
        assert '模型视觉推断如有生成，将单独标注' in row.summary


def test_index_failure_rolls_back_source_file_and_record(import_target, monkeypatch):
    from scripts.import_nmm import import_directory
    import app.ingestion
    source, engine, storage = import_target
    (source / '图.png').write_bytes(PNG)
    def fail(*args, **kwargs):
        raise RuntimeError('index failed')
    monkeypatch.setattr(app.ingestion, 'chunk_record', fail)
    result = import_directory(source, engine.url.render_as_string(hide_password=False), storage, ocr=lambda path: '原文')
    assert result['counts']['failed'] == 1
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Technique)) == 0
        assert db.scalar(select(func.count()).select_from(Source)) == 0
    assert not list(storage.glob('*'))


def test_tesseract_preserves_line_breaks_and_reports_missing_language(tmp_path):
    from scripts.import_nmm import TesseractOCR
    tool = tmp_path / 'tesseract'
    tool.write_text('#!/bin/sh\nprintf "高 光\\nAK11190 paint\\n"\n')
    tool.chmod(0o700)
    assert TesseractOCR(executable=str(tool))(tmp_path / 'image.png') == '高光\nAK11190 paint'
    tool.write_text('#!/bin/sh\nexit 1\n')
    with pytest.raises(RuntimeError, match='installed language data'):
        TesseractOCR(executable=str(tool))(tmp_path / 'image.png')


def test_cli_dry_run_writes_only_report_and_nonzero_on_failure(tmp_path):
    from scripts.import_nmm import main
    source = tmp_path / 'input'
    source.mkdir()
    (source / '图.png').write_bytes(PNG)
    report = tmp_path / 'report.json'
    args = ['--directory', str(source), '--database-url', test_database_url(f'{tmp_path}/missing.db'),
            '--storage-dir', str(tmp_path / 'attachments'), '--report', str(report)]
    assert main(args) == 0
    assert report.exists()
    assert not (tmp_path / 'missing.db').exists()
    assert main([*args, '--apply']) == 1
