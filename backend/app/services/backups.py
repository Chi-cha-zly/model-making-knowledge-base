"""Validated archive import/export with transactional catalog restore."""

import hashlib
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from fastapi import HTTPException
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session, selectinload
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from ..models import (
    Attachment,
    Product,
    ProductAlias,
    Source,
    Technique,
    product_attachments,
    product_sources,
    technique_attachments,
    technique_sources,
)
from ..chat import export_chat, validate_chat_backup, restore_chat
from ..evaluation import export_evaluation, validate_evaluation_backup, restore_evaluation
from ..experiments import export_experiments, validate_experiment_backup, restore_experiments
from ..ingestion import backfill, clear_index
from ..job_models import Job
from ..vision_models import ImageAnalysis, ImageDescription
from ..content import content_adapter, legacy_steps_to_blocks, image_ids
from .serializers import _source_out, _technique_out, _product_out, _attachment_out
from .catalog import _key, _aliases


class ImageAnalysisArchive(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    attachment_id: StrictInt = Field(gt=0)
    image_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    model: str = Field(min_length=1, max_length=200)
    ocr_text: str = Field(max_length=100000)
    description: ImageDescription
    updated_at: str


class BackupService:
    def __init__(self, engine, storage: Path, backup_limit: int):
        self.engine = engine
        self.storage = storage
        self.backup_limit = backup_limit

    def export_backup(self):
        storage, engine = self.storage, self.engine
        backup_limit = self.backup_limit
        with Session(engine) as db:
            payload = {
                'version': 6,
                'image_analyses': [
                    {
                        **{c.name: getattr(row, c.name) for c in ImageAnalysis.__table__.columns},
                        'updated_at': row.updated_at.isoformat(),
                    }
                    for row in db.scalars(
                        select(ImageAnalysis)
                        .join(Attachment)
                        .where(ImageAnalysis.image_hash == Attachment.sha256)
                        .order_by(ImageAnalysis.attachment_id)
                    )
                ],
                **export_experiments(db),
                **export_evaluation(db),
                **export_chat(db),
                'sources': [
                    _source_out(x) for x in db.scalars(select(Source).order_by(Source.id)).all()
                ],
                'techniques': [
                    _technique_out(x)
                    for x in db.scalars(
                        select(Technique)
                        .options(
                            selectinload(Technique.sources), selectinload(Technique.attachments)
                        )
                        .order_by(Technique.id)
                    ).all()
                ],
                'products': [
                    _product_out(x)
                    for x in db.scalars(
                        select(Product)
                        .options(
                            selectinload(Product.sources),
                            selectinload(Product.aliases),
                            selectinload(Product.attachments),
                        )
                        .order_by(Product.id)
                    ).all()
                ],
                'attachments': [
                    {
                        **_attachment_out(x),
                        'storage_name': x.storage_name,
                        'sha256': x.sha256,
                        'owner_id': x.owner_id,
                        'visibility': x.visibility,
                        'status': x.status,
                    }
                    for x in db.scalars(select(Attachment).order_by(Attachment.id)).all()
                ],
            }
            manifest = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            # ZipFile uses ZIP_STORED here, so payload bytes are a lower bound
            # on the final archive size. Reject oversized data before reading files.
            expanded = len(manifest) + sum(item['size'] for item in payload['attachments'])
            if expanded > backup_limit:
                raise HTTPException(413, 'Backup exceeds restore size limit')
            archive_path = storage / f'backup-{uuid.uuid4().hex}.zip'
            try:
                with ZipFile(archive_path, 'w') as archive:
                    archive.writestr('manifest.json', manifest)
                    for item in payload['attachments']:
                        path = storage / item['storage_name']
                        if not path.is_file():
                            raise HTTPException(500, 'Attachment file missing')
                        archive.write(path, f'attachments/{item["storage_name"]}')
                if archive_path.stat().st_size > backup_limit:
                    raise HTTPException(413, 'Backup exceeds restore size limit')
            except Exception:
                archive_path.unlink(missing_ok=True)
                raise
            return archive_path

    def restore_backup_archive(self, upload_dir: Path):
        storage, engine = self.storage, self.engine
        backup_limit = self.backup_limit
        archive_path = upload_dir / 'backup.zip'
        validated = upload_dir / 'validated'
        validated.mkdir()
        try:
            with ZipFile(archive_path) as archive:
                infos = archive.infolist()
                if len(infos) > 10000 or sum(x.file_size for x in infos) > backup_limit * 2:
                    raise ValueError('Backup expands beyond size limit')
                if len({x.filename for x in infos}) != len(infos):
                    raise ValueError('Duplicate backup entry')
                for info in infos:
                    path = PurePosixPath(info.filename)
                    if (
                        path.is_absolute()
                        or '..' in path.parts
                        or info.filename.startswith('\\')
                        or (info.external_attr >> 16) & 0o170000 == 0o120000
                    ):
                        raise ValueError('Unsafe backup path')
                if [x.filename for x in infos].count('manifest.json') != 1:
                    raise ValueError('Missing or duplicate manifest')
                if archive.getinfo('manifest.json').file_size > min(backup_limit, 64 * 1024 * 1024):
                    raise ValueError('Manifest exceeds size limit')
                data = json.loads(archive.read('manifest.json'))
                if data.get('version') not in (1, 2, 3, 4, 5, 6):
                    raise ValueError('Unsupported backup version')
                validate_chat_backup(data)
                validate_evaluation_backup(data)
                validate_experiment_backup(data)
                attachments = {}
                for item in data['attachments']:
                    name = item['storage_name']
                    if not re.fullmatch(r'[0-9a-f]{32}\.(png|jpg|gif|webp)', name):
                        raise ValueError('Unsafe attachment name')
                    digest = hashlib.sha256()
                    size = 0
                    with (
                        archive.open(f'attachments/{name}') as source,
                        (validated / name).open('wb') as output,
                    ):
                        while chunk := source.read(1024 * 1024):
                            size += len(chunk)
                            digest.update(chunk)
                            output.write(chunk)
                    if digest.hexdigest() != item['sha256'] or size != item['size']:
                        raise ValueError('Attachment checksum mismatch')
                    attachments[name] = size
                allowed = {'manifest.json', *(f'attachments/{name}' for name in attachments)}
                if {x.filename for x in infos} != allowed:
                    raise ValueError('Unexpected backup entry')
                source_ids = {x['id'] for x in data['sources']}
                if any(
                    not set(x['source_ids']) <= source_ids
                    for kind in ('techniques', 'products')
                    for x in data[kind]
                ):
                    raise ValueError('Broken source reference')
                attachment_ids = {x['id'] for x in data['attachments']}
                if any(
                    not set(x.get('attachment_ids', [])) <= attachment_ids
                    for kind in ('techniques', 'products')
                    for x in data[kind]
                ):
                    raise ValueError('Broken attachment reference')
                attachment_records = {a['id']: a for a in data['attachments']}
                restored_analyses = []
                if data['version'] >= 6:
                    seen_analysis_ids = set()
                    if not isinstance(data.get('image_analyses'), list):
                        raise ValueError('Missing image analyses')
                    for raw in data['image_analyses']:
                        item = ImageAnalysisArchive.model_validate(raw).model_dump()
                        aid = item['attachment_id']
                        if aid in seen_analysis_ids or aid not in attachment_records:
                            raise ValueError('Invalid image analysis attachment')
                        seen_analysis_ids.add(aid)
                        if item['image_hash'] != attachment_records[aid]['sha256']:
                            raise ValueError('Image analysis hash mismatch')
                        item['updated_at'] = datetime.fromisoformat(item['updated_at'])
                        restored_analyses.append(item)
                for item in data['techniques']:
                    raw_blocks = item.get('content_blocks', [])
                    if 'content_blocks' not in item or (raw_blocks == [] and 'steps' in item):
                        raw_blocks = legacy_steps_to_blocks(item.get('steps', []))
                    blocks = content_adapter.validate_python(raw_blocks)
                    item['content_blocks'] = content_adapter.dump_python(blocks)
                    used = image_ids(item['content_blocks'])
                    if not set(used) <= set(item.get('attachment_ids', [])):
                        raise ValueError('Broken content block attachment reference')
                    if any(
                        attachment_records[aid].get('owner_id') != 'local'
                        or attachment_records[aid].get('status') != 'published'
                        for aid in used
                    ):
                        raise ValueError('Inactive content block attachment')
                    item.pop('steps', None)

        except OSError as exc:
            raise HTTPException(500, 'Restore could not stage attachments') from exc
        except (ValueError, KeyError, TypeError, json.JSONDecodeError, RuntimeError) as exc:
            raise HTTPException(400, f'Invalid backup: {exc}')
        except Exception as exc:
            from zipfile import BadZipFile

            if isinstance(exc, BadZipFile):
                raise HTTPException(400, 'Invalid ZIP backup')
            raise
        # Stage all files before changing the database. Keep copies of existing
        # files so a failed replace or commit can restore the previous state.
        try:
            with TemporaryDirectory(dir=storage, prefix='restore-') as temp_name:
                temp = Path(temp_name)
                incoming = temp / 'incoming'
                previous = temp / 'previous'
                incoming.mkdir()
                previous.mkdir()
                for name in attachments:
                    os.replace(validated / name, incoming / name)
                    target = storage / name
                    if target.exists():
                        os.link(target, previous / name)
                with Session(engine) as db:
                    placed = []
                    try:
                        clear_index(db)
                        db.execute(delete(technique_attachments))
                        db.execute(delete(product_attachments))
                        db.execute(delete(technique_sources))
                        db.execute(delete(product_sources))
                        db.query(ProductAlias).delete()
                        db.query(Technique).delete()
                        db.query(Product).delete()
                        db.query(Source).delete()
                        db.query(Attachment).delete()
                        for item in data['sources']:
                            fields = {
                                k: item.get(k)
                                for k in (
                                    'id',
                                    'title',
                                    'url',
                                    'source_type',
                                    'author',
                                    'license_note',
                                    'content_hash',
                                    'owner_id',
                                    'visibility',
                                    'status',
                                )
                            }
                            fields['retrieved_at'] = (
                                datetime.fromisoformat(item['retrieved_at'])
                                if item.get('retrieved_at')
                                else None
                            )
                            db.add(Source(**fields))
                        for item in data['attachments']:
                            fields = {
                                k: item.get(k)
                                for k in (
                                    'id',
                                    'filename',
                                    'content_type',
                                    'storage_name',
                                    'sha256',
                                    'size',
                                    'owner_id',
                                    'visibility',
                                    'status',
                                )
                            }
                            db.add(Attachment(**fields))
                        db.flush()
                        for item in data['techniques']:
                            fields = {
                                k: item.get(k)
                                for k in (
                                    'id',
                                    'title',
                                    'category',
                                    'summary',
                                    'content_blocks',
                                    'prerequisites',
                                    'risks',
                                    'difficulty',
                                    'tags',
                                    'owner_id',
                                    'visibility',
                                    'status',
                                )
                            }
                            db.add(
                                Technique(
                                    **fields,
                                    sources=[db.get(Source, sid) for sid in item['source_ids']],
                                    attachments=[
                                        db.get(Attachment, aid)
                                        for aid in item.get('attachment_ids', [])
                                    ],
                                )
                            )
                        for item in data['products']:
                            fields = {
                                k: item.get(k)
                                for k in (
                                    'id',
                                    'brand',
                                    'name',
                                    'sku',
                                    'kind',
                                    'properties',
                                    'manufacturer_url',
                                    'owner_id',
                                    'visibility',
                                    'status',
                                )
                            }
                            product = Product(
                                **fields,
                                sku_key=_key(item.get('sku')) or None,
                                sources=[db.get(Source, sid) for sid in item['source_ids']],
                                attachments=[
                                    db.get(Attachment, aid)
                                    for aid in item.get('attachment_ids', [])
                                ],
                            )
                            _aliases(db, product, item['aliases'])
                            db.add(product)
                        restore_chat(db, data)
                        restore_evaluation(db, data)
                        if data.get('version', 1) >= 4:
                            # Restored experiment IDs may repeat; stale idempotency keys must not bind to new rows.
                            db.execute(delete(Job))
                        else:
                            db.execute(delete(Job).where(Job.kind != 'experiment'))
                        restore_experiments(db, data)
                        db.flush()
                        db.add_all(ImageAnalysis(**item) for item in restored_analyses)
                        db.flush()
                        # Restore is all-or-nothing, including every derived index.
                        backfill(db, strict=True)
                        if engine.dialect.name == 'postgresql':
                            for table in (
                                'sources',
                                'techniques',
                                'products',
                                'product_aliases',
                                'attachments',
                                'conversations',
                                'messages',
                                'runs',
                                'evaluation_runs',
                                'experiment_jobs',
                            ):
                                sequence = db.scalar(
                                    text("SELECT pg_get_serial_sequence(:table, 'id')"),
                                    {'table': table},
                                )
                                next_id = db.scalar(
                                    text(f'SELECT COALESCE(MAX(id), 0) + 1 FROM {table}')
                                )
                                # Unlike setval, RESTART rolls back if file placement or commit fails.
                                db.execute(
                                    text(f'ALTER SEQUENCE {sequence} RESTART WITH {int(next_id)}')
                                )
                        for name in attachments:
                            os.replace(incoming / name, storage / name)
                            placed.append(name)
                        db.commit()
                    except Exception as exc:
                        db.rollback()
                        for name in reversed(placed):
                            target = storage / name
                            saved = previous / name
                            if saved.exists():
                                os.replace(saved, target)
                            else:
                                target.unlink(missing_ok=True)
                        if isinstance(exc, OSError):
                            raise HTTPException(500, 'Restore could not write attachments') from exc
                        raise HTTPException(400, 'Backup data is invalid') from exc
        except OSError as exc:
            raise HTTPException(500, 'Restore could not stage attachments') from exc
        return {
            'restored': True,
            'sources': len(data['sources']),
            'techniques': len(data['techniques']),
            'products': len(data['products']),
            'attachments': len(attachments),
        }
