"""Offline vision worker tests with real PostgreSQL atomic publication."""

import asyncio
import hashlib

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from pg_test_support import test_database_url
from app.models import Attachment, Source, Technique
from app.ingestion_models import Chunk, Document
from app.vision_models import ImageAnalysis
from app.services.ingestion import IngestionService

DESCRIPTION = {
    'description': '金属外观的肩甲边缘有浅色高光',
    'materials': ['金属外观'],
    'techniques': ['边缘提亮'],
    'colors': ['蓝灰色', '白色'],
    'step_number': None,
    'visible_text': '高光',
    'uncertainties': ['实际漆料无法确认'],
}
RAW = b'\x89PNG\r\n\x1a\nfixture'


class Provider:
    vision_configured = True
    vision_model = 'fixture-vision'

    def __init__(self, callback=None):
        self.callback = callback
        self.calls = 0

    async def describe_image(self, image, content_type):
        assert image == RAW and content_type == 'image/png'
        self.calls += 1
        if self.callback:
            await self.callback()
        return dict(DESCRIPTION)


def seed(tmp_path, *, linked=1):
    engine = create_engine(test_database_url(str(tmp_path)))
    storage = tmp_path / 'images'
    storage.mkdir()
    filename = 'a' * 32 + '.png'
    (storage / filename).write_bytes(RAW)
    with Session(engine) as db:
        source = Source(title='Reference')
        image = Attachment(
            filename='image.png',
            storage_name=filename,
            content_type='image/png',
            sha256=hashlib.sha256(RAW).hexdigest(),
            size=len(RAW),
        )
        for index in range(linked):
            db.add(
                Technique(
                    title=f'Metal {index}',
                    sources=[source],
                    attachments=[image],
                    content_blocks=[{'type': 'image', 'attachment_id': 1, 'caption': '人工说明'}],
                )
            )
        IngestionService(db).backfill()
        db.commit()
    return engine, storage


def state(engine):
    with Session(engine) as db:
        analyses = [
            (a.attachment_id, a.model, a.ocr_text, a.description)
            for a in db.scalars(select(ImageAnalysis))
        ]
        versions = [
            (d.id, d.active_version) for d in db.scalars(select(Document).order_by(Document.id))
        ]
        return analyses, versions


def test_vision_dry_run_and_unconfigured_do_not_ocr_or_call_model(tmp_path):
    from app.vision_jobs import describe_pending

    engine, storage = seed(tmp_path)
    provider = Provider()

    def forbidden(_):
        raise AssertionError('Dry run must not run OCR')

    before = state(engine)
    result = asyncio.run(describe_pending(engine, provider, storage, ocr=forbidden))
    assert result['status'] == 'dry_run' and result['documents'][0]['attachment_id'] == 1
    provider.vision_configured = False
    result = asyncio.run(describe_pending(engine, provider, storage, apply=True, ocr=forbidden))
    assert result['status'] == 'unconfigured'
    assert provider.calls == 0 and state(engine) == before


def test_vision_success_reindexes_all_linked_records_and_is_idempotent(tmp_path):
    from app.vision_jobs import describe_pending

    engine, storage = seed(tmp_path, linked=2)

    async def outside_transaction():
        assert engine.pool.checkedout() == 0

    provider = Provider(outside_transaction)
    before = state(engine)[1]
    progress = []
    result = asyncio.run(
        describe_pending(
            engine,
            provider,
            storage,
            apply=True,
            ocr=lambda path: '真实 OCR 文字',
            progress=lambda done, total: progress.append((done, total)),
        )
    )
    assert result['status'] == 'completed'
    assert result['documents'][0]['status'] == 'ready'
    analyses, versions = state(engine)
    assert analyses == [(1, 'fixture-vision', '真实 OCR 文字', DESCRIPTION)]
    assert versions == [(id, version + 1) for id, version in before]
    with Session(engine) as db:
        texts = list(
            db.scalars(
                select(Chunk.content).join(Document).where(Chunk.version == Document.active_version)
            )
        )
        assert any('本地 OCR 原文' in text and '真实 OCR 文字' in text for text in texts)
        assert any('模型视觉推断' in text and '金属外观的肩甲' in text for text in texts)
        assert not any('真实 OCR 文字' in text and '金属外观的肩甲' in text for text in texts)
        assert all(
            t.content_blocks[0]['caption'] == '人工说明' for t in db.scalars(select(Technique))
        )
    repeated = asyncio.run(
        describe_pending(engine, provider, storage, apply=True, ocr=lambda path: 'not called')
    )
    assert (
        repeated['documents'] == []
        and provider.calls == 1
        and state(engine) == (analyses, versions)
    )
    assert progress == [(1, 1)]


def test_vision_model_failure_preserves_old_analysis_and_index(tmp_path):
    from app.vision_jobs import describe_pending

    engine, storage = seed(tmp_path)
    asyncio.run(
        describe_pending(engine, Provider(), storage, apply=True, ocr=lambda path: 'old OCR')
    )
    before = state(engine)

    async def fail():
        raise RuntimeError('secret-upstream-body')

    provider = Provider(fail)
    provider.vision_model = 'changed-model'
    result = asyncio.run(
        describe_pending(engine, provider, storage, apply=True, ocr=lambda path: 'new OCR')
    )
    assert result['status'] == 'partial_failure' and result['documents'][0]['status'] == 'failed'
    assert 'secret-upstream-body' not in str(result) and state(engine) == before


@pytest.mark.parametrize('change', ['version', 'source', 'file'])
def test_vision_stale_inputs_never_publish(tmp_path, change):
    from app.vision_jobs import describe_pending

    engine, storage = seed(tmp_path)

    async def mutate():
        if change == 'file':
            next(storage.iterdir()).write_bytes(RAW + b'changed')
        else:
            with Session(engine) as db:
                if change == 'version':
                    row = db.get(Technique, 1)
                    row.summary = 'updated while the model was running'
                    IngestionService(db).reindex(row)
                else:
                    db.get(Source, 1).status = 'withdrawn'
                db.commit()

    result = asyncio.run(
        describe_pending(engine, Provider(mutate), storage, apply=True, ocr=lambda path: 'OCR')
    )
    assert result['documents'][0]['status'] == 'stale'
    assert state(engine)[0] == []


def test_vision_cancel_during_model_call_does_not_publish(tmp_path):
    from app.vision_jobs import describe_pending

    engine, storage = seed(tmp_path)
    before = state(engine)

    async def scenario():
        entered = asyncio.Event()

        async def block():
            entered.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(
            describe_pending(engine, Provider(block), storage, apply=True, ocr=lambda path: 'OCR')
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert state(engine) == before


def test_vision_index_failure_rolls_back_every_linked_record(tmp_path, monkeypatch):
    from app import vision_jobs

    engine, storage = seed(tmp_path, linked=2)
    before = state(engine)
    real = IngestionService.reindex
    calls = []

    def fail_second(service, record, **kwargs):
        calls.append(record.id)
        if len(calls) == 2:
            raise RuntimeError('index failure')
        return real(service, record, **kwargs)

    monkeypatch.setattr(IngestionService, 'reindex', fail_second)
    result = asyncio.run(
        vision_jobs.describe_pending(
            engine, Provider(), storage, apply=True, ocr=lambda path: 'OCR'
        )
    )
    assert result['documents'][0]['status'] == 'failed' and state(engine) == before


def test_vision_excludes_withdrawn_sources_before_outbound_request(tmp_path):
    from app.vision_jobs import describe_pending

    engine, storage = seed(tmp_path)
    with Session(engine) as db:
        db.get(Source, 1).status = 'withdrawn'
        db.commit()
    provider = Provider()
    result = asyncio.run(
        describe_pending(engine, provider, storage, apply=True, ocr=lambda path: 'OCR')
    )
    assert result['documents'] == [] and provider.calls == 0


def test_vision_ocr_failure_never_calls_model_or_changes_index(tmp_path):
    from app.vision_jobs import describe_pending

    engine, storage = seed(tmp_path)
    before = state(engine)
    provider = Provider()

    def fail(_):
        raise RuntimeError('local OCR failed')

    result = asyncio.run(describe_pending(engine, provider, storage, apply=True, ocr=fail))
    assert result['documents'][0]['status'] == 'failed'
    assert provider.calls == 0 and state(engine) == before


def test_vision_withdrawal_during_local_ocr_prevents_upload(tmp_path):
    from app.vision_jobs import describe_pending

    engine, storage = seed(tmp_path)
    provider = Provider()

    def withdraw(_):
        with Session(engine) as db:
            db.get(Source, 1).status = 'withdrawn'
            db.commit()
        return 'OCR'

    result = asyncio.run(describe_pending(engine, provider, storage, apply=True, ocr=withdraw))
    assert result['documents'][0]['status'] == 'stale'
    assert provider.calls == 0 and state(engine)[0] == []


def test_vision_image_limit_bounds_requests_and_next_run_resumes(tmp_path):
    from app.vision_jobs import describe_pending

    engine, storage = seed(tmp_path)
    filename = 'b' * 32 + '.png'
    (storage / filename).write_bytes(RAW)
    with Session(engine) as db:
        attachment = Attachment(
            filename='second.png',
            storage_name=filename,
            content_type='image/png',
            sha256=hashlib.sha256(RAW).hexdigest(),
            size=len(RAW),
        )
        row = db.get(Technique, 1)
        row.attachments.append(attachment)
        db.flush()
        row.content_blocks = row.content_blocks + [
            {'type': 'image', 'attachment_id': attachment.id, 'caption': 'Second'}
        ]
        IngestionService(db).reindex(row)
        db.commit()
    provider = Provider()
    first = asyncio.run(
        describe_pending(
            engine, provider, storage, apply=True, max_documents=1, ocr=lambda _: 'OCR'
        )
    )
    assert len(first['documents']) == 1 and provider.calls == 1
    second = asyncio.run(
        describe_pending(
            engine, provider, storage, apply=True, max_documents=1, ocr=lambda _: 'OCR'
        )
    )
    assert (
        len(second['documents']) == 1
        and second['documents'][0]['attachment_id'] != first['documents'][0]['attachment_id']
    )
    assert provider.calls == 2 and len(state(engine)[0]) == 2


def test_vision_cli_requires_target_and_defaults_to_dry_run(tmp_path, capsys):
    from scripts.describe_images import main

    with pytest.raises(SystemExit) as missing:
        main([])
    assert missing.value.code == 2
    capsys.readouterr()
    engine, storage = seed(tmp_path)
    before = state(engine)
    code = main(
        [
            '--database-url',
            engine.url.render_as_string(hide_password=False),
            '--storage-dir',
            str(storage),
        ],
        environ={
            'VISION_BASE_URL': 'https://vision.invalid/v1',
            'VISION_API_KEY': 'fixture',
            'VISION_MODEL': 'fixture-vision',
        },
    )
    import json

    result = json.loads(capsys.readouterr().out)
    assert (
        code == 0
        and result['status'] == 'dry_run'
        and result['documents'][0]['status'] == 'planned'
    )
    assert state(engine) == before


def test_vision_quality_review_is_reported_without_blocking_vlm_or_indexing_raw(tmp_path):
    from app.vision_jobs import describe_pending

    engine, storage = seed(tmp_path)

    class Review:
        def recognize(self, path):
            return {
                'text': '',
                'status': 'needs_review',
                'raw_text': 'unreadable-noise',
                'engine': 'test',
            }

    provider = Provider()
    result = asyncio.run(describe_pending(engine, provider, storage, apply=True, ocr=Review()))
    assert result['documents'][0]['status'] == 'ready'
    assert result['documents'][0]['ocr_status'] == 'needs_review' and provider.calls == 1
    with Session(engine) as db:
        assert db.get(ImageAnalysis, 1).ocr_text == ''
        assert db.get(ImageAnalysis, 1).description == DESCRIPTION
        assert all('unreadable-noise' not in c.content for c in db.scalars(select(Chunk)))
