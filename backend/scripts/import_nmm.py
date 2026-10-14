"""Import selected local reference material with local OCR, never an LLM call.

Run from backend: python -m scripts.import_nmm --help
The target database must already have the current Alembic schema.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from uuid import uuid4

from sqlalchemy import select
from app.db import create_db_engine
from sqlalchemy.orm import Session

from app.models import Attachment, Source, Technique

IMAGE_TYPES = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
               '.gif': 'image/gif', '.webp': 'image/webp'}
SOURCE_TYPE = 'local-nmm-import'
OCR_NOTICE = '本地 OCR 原文（未经人工核对，可能包含错字）：\n'
EMPTY_NOTICE = '本地 OCR 未提取到可识别文字。请查看原图；模型视觉推断如有生成，将单独标注。'


from app.services.ocr import TesseractOCR, local_ocr, recognize_local

OCR_HEADING = '本地 OCR 原文（未经人工核对）：'
OCR_SUMMARY = '已提取本地 OCR 正文（未经人工核对），请对照原图阅读。'
REVIEW_NOTICE = '本地 OCR 待人工核对：检测到不可靠的孤立字符或竖排文字，请查看原图。'
IMAGE_CAPTION = '原始参考图。OCR 正文见下方；模型视觉推断单独标注。'


def image_article(attachment_id, text, *, status=None):
    blocks = [{'type': 'image', 'attachment_id': attachment_id, 'caption': IMAGE_CAPTION}]
    if text:
        blocks.append({'type': 'heading', 'text': OCR_HEADING})
        blocks.extend({'type': 'paragraph', 'text': text[i:i+20000]} for i in range(0,len(text),20000))
    return (REVIEW_NOTICE if status == 'needs_review' else OCR_SUMMARY if text else EMPTY_NOTICE), blocks



def validate_report_path(path, *protected_directories):
    path=Path(path).resolve()
    if any(path.is_relative_to(Path(directory).resolve()) for directory in protected_directories):
        raise ValueError('Report must be outside input and attachment directories')
    return path


def write_report(path, result):
    import os
    import tempfile
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.ocr-report-',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as file:
            json.dump(result,file,ensure_ascii=False,indent=2)
            file.write('\n')
        os.replace(name,path)
    finally:
        Path(name).unlink(missing_ok=True)


def scan_directory(directory, markdown=()):
    directory = Path(directory).resolve(strict=True)
    if not directory.is_dir():
        raise ValueError('Input must be a directory')
    selected = set(markdown)
    if any(Path(name).name != name or not name.lower().endswith('.md') for name in selected):
        raise ValueError('--markdown must name a Markdown file directly inside the input directory')
    if any(not (directory / name).is_file() for name in selected):
        raise ValueError('Selected Markdown file does not exist')
    files = []
    for path in sorted(directory.iterdir(), key=lambda p: p.name):
        if path.is_symlink():
            files.append({'filename': path.name, 'status': 'skipped', 'reason': 'symlink'})
            continue
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        item = {'filename': path.name}
        if suffix not in IMAGE_TYPES and suffix != '.md':
            item.update(status='skipped', reason='unsupported_file')
        elif suffix == '.md' and path.name not in selected:
            item.update(status='skipped', reason='markdown_not_selected')
        else:
            item.update(status='planned', kind='markdown' if suffix == '.md' else 'image',
                        sha256=hashlib.sha256(path.read_bytes()).hexdigest(), bytes=path.stat().st_size)
        files.append(item)
    return files


def _title_author(path):
    match = re.match(r'^(.*?)_(\d+)_(.*?)_来自小红书网页版\d*$', path.stem)
    if match:
        return (match[1] or '未命名 NMM 参考') + f' · 图 {match[2]}', match[3]
    return path.stem, None


def _validate_image(raw, suffix):
    if len(raw) > 5 * 1024 * 1024:
        raise ValueError('Image exceeds the 5 MiB attachment limit')
    valid = (suffix in ('.jpg', '.jpeg') and raw.startswith(b'\xff\xd8\xff')
             or suffix == '.png' and raw.startswith(b'\x89PNG\r\n\x1a\n')
             or suffix == '.gif' and raw.startswith((b'GIF87a', b'GIF89a'))
             or suffix == '.webp' and raw.startswith(b'RIFF') and raw[8:12] == b'WEBP')
    if not valid:
        raise ValueError('Image signature does not match file extension')


def _import_one(db, path, item, storage, ocr):
    from app.ingestion import ingest_record
    existing = db.scalar(select(Technique).join(Technique.sources).where(
        Source.source_type == SOURCE_TYPE, Source.content_hash == item['sha256'],
        Source.owner_id == 'local', Technique.owner_id == 'local'))
    if existing:
        item.update(status='unchanged', technique_id=existing.id)
        return
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != item['sha256']:
        raise ValueError('Input changed after scanning; retry import')
    title, author = _title_author(path)
    if len(title) > 300 or len(path.name) > 300:
        raise ValueError('Source title or filename exceeds 300 characters')
    image = item['kind'] == 'image'
    if image:
        _validate_image(raw, path.suffix.lower())
        item['ocr_status'] = 'running'
        recognized = recognize_local(ocr,path)
        text = recognized['text'].strip()
        item['ocr_status'] = recognized['status']
        if recognized['status'] == 'needs_review':
            item['ocr_review'] = recognized
        if hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
            raise ValueError('Input changed during OCR; retry import')
        item['ocr_characters'] = len(text)
        body = OCR_NOTICE + text if text else EMPTY_NOTICE
    else:
        if len(raw) > 1024 * 1024:
            raise ValueError('Markdown exceeds 1 MiB')
        body = raw.decode('utf-8-sig').strip()
        if not body:
            raise ValueError('Markdown is empty')
        item['ocr_status'] = 'not_applicable'
    if len(body) > 100000:
        raise ValueError('Extracted content exceeds the article text limit')
    source = Source(title=path.name, source_type=SOURCE_TYPE, author=author,
                    content_hash=item['sha256'],
                    license_note='用户提供的本地参考资料；作者信息来自文件名，原始链接未知。仅作个人学习，不公开分发。')
    db.add(source)
    db.flush()
    destination = None
    try:
        attachments = []
        if image:
            attachment = Attachment(filename=path.name, content_type=IMAGE_TYPES[path.suffix.lower()],
                                    storage_name=uuid4().hex + path.suffix.lower(),
                                    sha256=item['sha256'], size=len(raw))
            db.add(attachment)
            db.flush()
            attachments = [attachment]
            body, blocks = image_article(attachment.id, text, status=item['ocr_status'])
            storage.mkdir(parents=True, exist_ok=True)
            destination = storage / attachment.storage_name
            destination.write_bytes(raw)
        else:
            blocks = [{'type': 'paragraph', 'text': body[i:i + 20000]} for i in range(0, len(body), 20000)]
        record = Technique(title=title, category='NMM 参考', summary=body, content_blocks=blocks,
                           tags=['NMM', '本地OCR' if image else '知识正文'], sources=[source], attachments=attachments)
        db.add(record)
        db.flush()
        document = ingest_record(db, record)
        if document.status != 'ready':
            raise RuntimeError('Knowledge indexing failed')
        db.commit()
        item.update(status='imported', technique_id=record.id, source_id=source.id,
                    attachment_ids=[a.id for a in attachments], vision_status='not_generated',
                    embedding_status='pending')
    except BaseException:
        db.rollback()
        if destination:
            destination.unlink(missing_ok=True)
        raise


def import_directory(directory, database_url, storage_dir, *, markdown=(), dry_run=False, ocr=None):
    files = scan_directory(directory, markdown)
    report = {'dry_run': dry_run, 'directory': str(Path(directory).resolve()),
              'files': files, 'counts': {}}
    # Dry-run intentionally does not connect to or initialize any target.
    if not dry_run:
        engine = create_db_engine(database_url)
        try:
            for item in files:
                if item['status'] != 'planned':
                    continue
                with Session(engine) as db:
                    try:
                        _import_one(db, Path(directory) / item['filename'], item,
                                    Path(storage_dir).resolve(), ocr or local_ocr())
                    except Exception as exc:
                        db.rollback()
                        if item.get('ocr_status') == 'running':
                            item['ocr_status'] = 'failed'
                        item.update(status='failed', error_type=type(exc).__name__,
                                    error='Import failed; verify schema, OCR, input and storage permissions')
        finally:
            engine.dispose()
    report['counts'] = {status: sum(f['status'] == status for f in files)
                        for status in ('planned', 'imported', 'unchanged', 'skipped', 'failed')}
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--database-url', required=True, help='Explicit target; schema must be migrated first')
    parser.add_argument('--storage-dir', required=True, type=Path)
    parser.add_argument('--markdown', action='append', default=[], help='Explicit knowledge Markdown filename; repeatable')
    parser.add_argument('--apply', action='store_true', help='Write target; default is a no-write dry-run')
    parser.add_argument('--dry-run', action='store_true', help='Explicit no-write planning mode')
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--ocr-engine', choices=['vision','tesseract'], default='vision')
    parser.add_argument('--tessdata-dir', type=Path)
    parser.add_argument('--languages', default='chi_sim+eng')
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        parser.error('--apply and --dry-run are mutually exclusive')
    try:
        report_path=validate_report_path(args.report,args.directory,args.storage_dir)
        result = import_directory(args.directory, args.database_url, args.storage_dir,
                                  markdown=args.markdown, dry_run=not args.apply,
                                  ocr=local_ocr(engine=args.ocr_engine, languages=args.languages, tessdata_dir=args.tessdata_dir))
    except (OSError, ValueError):
        print('Cannot plan import: check input directory and explicit Markdown selection.', file=sys.stderr)
        return 2
    write_report(report_path,result)
    print(json.dumps(result['counts'], ensure_ascii=False))
    return 1 if result['counts']['failed'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
