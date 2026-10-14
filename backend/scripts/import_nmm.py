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
import subprocess
import sys
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import Attachment, Source, Technique

IMAGE_TYPES = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
               '.gif': 'image/gif', '.webp': 'image/webp'}
SOURCE_TYPE = 'local-nmm-import'
OCR_NOTICE = '本地 OCR 原文（未经人工核对，可能包含错字；视觉描述尚未生成）：\n'
EMPTY_NOTICE = '本地 OCR 未提取到可识别文字；视觉描述尚未生成。仅按文件名元数据检索，请查看原图。'


class TesseractOCR:
    def __init__(self, *, executable='tesseract', languages='chi_sim+eng', tessdata_dir=None, timeout=90):
        self.executable = executable
        self.languages = languages
        self.tessdata_dir = Path(tessdata_dir).resolve() if tessdata_dir else None
        self.timeout = timeout

    def __call__(self, path):
        command = [self.executable, str(Path(path).resolve()), 'stdout', '-l', self.languages,
                   '--oem', '1', '--psm', '11']
        if self.tessdata_dir:
            command += ['--tessdata-dir', str(self.tessdata_dir)]
        try:
            result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                    check=False, timeout=self.timeout)
        except FileNotFoundError:
            raise RuntimeError('Tesseract executable is unavailable') from None
        except subprocess.TimeoutExpired:
            raise RuntimeError('Tesseract OCR timed out') from None
        if result.returncode:
            raise RuntimeError('Tesseract OCR failed; verify image and installed language data')
        # Tesseract may put spaces between every Chinese character. Join only adjacent
        # CJK characters; preserve Latin words, paint identifiers and line boundaries.
        return re.sub(r'(?<=[\u3400-\u9fff])[^\S\n]+(?=[\u3400-\u9fff])', '', result.stdout).strip()


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
        text = ocr(path).strip()
        item['ocr_status'] = 'completed' if text else 'empty'
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
            # Each OCR section retains the same original-image association.
            blocks = [{'type': 'image', 'attachment_id': attachment.id, 'caption': body[i:i + 1000]}
                      for i in range(0, len(body), 1000)]
            storage.mkdir(parents=True, exist_ok=True)
            destination = storage / attachment.storage_name
            destination.write_bytes(raw)
        else:
            blocks = [{'type': 'paragraph', 'text': body[i:i + 20000]} for i in range(0, len(body), 20000)]
        record = Technique(title=title, category='NMM 参考', summary=body, content_blocks=blocks,
                           steps=[b.get('caption', b.get('text', '')) for b in blocks],
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
        engine = create_engine(database_url)
        try:
            for item in files:
                if item['status'] != 'planned':
                    continue
                with Session(engine) as db:
                    try:
                        _import_one(db, Path(directory) / item['filename'], item,
                                    Path(storage_dir).resolve(), ocr or TesseractOCR())
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
    parser.add_argument('--tessdata-dir', type=Path)
    parser.add_argument('--languages', default='chi_sim+eng')
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        parser.error('--apply and --dry-run are mutually exclusive')
    try:
        result = import_directory(args.directory, args.database_url, args.storage_dir,
                                  markdown=args.markdown, dry_run=not args.apply,
                                  ocr=TesseractOCR(languages=args.languages, tessdata_dir=args.tessdata_dir))
    except (OSError, ValueError):
        print('Cannot plan import: check input directory and explicit Markdown selection.', file=sys.stderr)
        return 2
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result['counts'], ensure_ascii=False))
    return 1 if result['counts']['failed'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
