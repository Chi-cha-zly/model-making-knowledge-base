"""Explicit one-way legacy import. Source is read-only; target must be empty.

Run Alembic against a new PostgreSQL database first. This command never changes
.env, the SQLite database, or its attachments. Review the dry run before --apply.
"""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from sqlalchemy import MetaData, create_engine, select, text
from pgvector.sqlalchemy import Vector as Vector  # registers PostgreSQL reflection
from app.content import legacy_steps_to_blocks
from app.db import create_db_engine, init_db
from sqlalchemy.orm import Session
from sqlalchemy.sql.ddl import sort_tables
from app.services.ingestion import IngestionService


def copy_legacy_database(
    source_path, source_attachments, target_url, target_attachments, *, apply=False
):
    source_path = Path(source_path).resolve(strict=True)
    source_attachments = Path(source_attachments).resolve(strict=True)
    target_attachments = Path(target_attachments).resolve()
    if source_attachments == target_attachments or source_attachments in target_attachments.parents:
        raise ValueError('Target attachments must be separate from the source')
    if target_attachments.exists() and any(target_attachments.iterdir()):
        raise ValueError('Target attachments must be empty')
    source = create_engine(f'sqlite:///file:{source_path.as_posix()}?mode=ro&uri=true')
    target = create_db_engine(target_url)
    init_db(target)
    source_meta, target_meta = MetaData(), MetaData()
    source_meta.reflect(source)
    target_meta.reflect(target)
    # SQLite predates NMM and has no defined NMM/checkpoint migration format.
    unsupported = (set(source_meta.tables) - set(target_meta.tables) - {'alembic_version'}) | (
        set(source_meta.tables)
        & {
            'nmm_projects',
            'nmm_assets',
            'nmm_plans',
            'nmm_steps',
            'checkpoints',
            'checkpoint_blobs',
            'checkpoint_writes',
            'checkpoint_migrations',
        }
    )
    records = {}
    placed = []
    try:
        if unsupported:
            raise ValueError('Unsupported source tables: ' + ', '.join(sorted(unsupported)))
        with source.connect() as conn:
            conn.exec_driver_sql('BEGIN')
            for name, table in source_meta.tables.items():
                if name != 'alembic_version':
                    records[name] = [dict(row) for row in conn.execute(select(table)).mappings()]
        files = []
        for attachment in records.get('attachments', []):
            name = attachment['storage_name']
            if Path(name).name != name or not name or (source_attachments / name).is_symlink():
                raise ValueError('Unsafe attachment path')
            path = (source_attachments / name).resolve(strict=True)
            if path.parent != source_attachments:
                raise ValueError('Unsafe attachment path')
            if (
                path.stat().st_size != attachment['size']
                or hashlib.sha256(path.read_bytes()).hexdigest() != attachment['sha256']
            ):
                raise ValueError('Attachment checksum mismatch')
            files.append(path)
        attachment_hashes = {a['id']: a['sha256'] for a in records.get('attachments', [])}
        for chunk in records.get('chunks', []):
            if chunk.get('attachment_id') in attachment_hashes:
                chunk['payload'] = {
                    **chunk['payload'],
                    'attachment_hash': attachment_hashes[chunk['attachment_id']],
                }
        for row in records.get('techniques', []):
            if not row.get('content_blocks'):
                row['content_blocks'] = legacy_steps_to_blocks(row.get('steps', []))
            row.pop('steps', None)
        report = {
            'status': 'dry_run',
            'source': str(source_path),
            'target': target.url.render_as_string(hide_password=True),
            'attachment_target': str(target_attachments),
            'tables': {name: len(rows) for name, rows in records.items()},
            'attachments': len(files),
        }
        with target.begin() as conn:
            # Emptiness applies to the entire target, including newer domains.
            # It needs no dependency ordering and must not skip non-source tables.
            for table in target_meta.tables.values():
                if table.name in ('alembic_version', 'checkpoint_migrations'):
                    continue
                if conn.execute(select(table).limit(1)).first():
                    raise ValueError('Target database must be empty')
            if not apply:
                return report
            # Reflected NMM tables contain a real project/asset FK cycle. NMM is
            # unsupported above; sort only the tables this legacy input copies.
            tables = sort_tables([target_meta.tables[name] for name in records])
            target_attachments.parent.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(
                dir=target_attachments.parent, prefix='legacy-stage-'
            ) as staging:
                for path in files:
                    staged = Path(staging) / path.name
                    shutil.copy2(path, staged)
                    expected = next(
                        a for a in records['attachments'] if a['storage_name'] == path.name
                    )
                    if (
                        staged.stat().st_size != expected['size']
                        or hashlib.sha256(staged.read_bytes()).hexdigest() != expected['sha256']
                    ):
                        raise ValueError('Attachment changed during copy')
                for table in tables:
                    rows = records.get(table.name, [])
                    if rows:
                        columns = set(table.c.keys())
                        conn.execute(
                            table.insert(),
                            [
                                {key: value for key, value in row.items() if key in columns}
                                for row in rows
                            ],
                        )
                for table in tables:
                    if 'id' in table.c and table.c.id.type.python_type is int:
                        sequence = conn.scalar(
                            text('SELECT pg_get_serial_sequence(:table, :column)'),
                            {'table': table.name, 'column': 'id'},
                        )
                        if sequence:
                            maximum = conn.scalar(
                                select(__import__('sqlalchemy').func.max(table.c.id))
                            )
                            conn.execute(
                                text(
                                    f'ALTER SEQUENCE {sequence} RESTART WITH {int(maximum or 0) + 1}'
                                )
                            )
                with Session(bind=conn) as db:
                    report['indexed_documents'] = IngestionService(db).backfill(strict=True)
                target_attachments.mkdir(parents=True, exist_ok=True)
                for path in files:
                    destination = target_attachments / path.name
                    (Path(staging) / path.name).replace(destination)
                    placed.append(destination)
        report['status'] = 'completed'
        return report
    except Exception:
        for path in reversed(placed):
            path.unlink(missing_ok=True)
        raise
    finally:
        source.dispose()
        target.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-sqlite', required=True)
    parser.add_argument('--source-attachments', required=True)
    parser.add_argument('--database-url', required=True)
    parser.add_argument('--target-attachments', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    report = copy_legacy_database(
        args.source_sqlite,
        args.source_attachments,
        args.database_url,
        args.target_attachments,
        apply=args.apply,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
