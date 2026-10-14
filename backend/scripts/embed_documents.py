"""Explicit embedding CLI. No .env loading and no default database or paid call."""

import argparse
import asyncio
import json
import os

from app.db import create_db_engine

from app.embedding_jobs import embed_pending
from app.llm import LangChainProvider


def main(argv=None, *, environ=None):
    parser = argparse.ArgumentParser(
        description='Plan pending embeddings; --apply explicitly invokes the configured model.'
    )
    parser.add_argument(
        '--database-url', required=True, help='Explicit target database with current Alembic schema'
    )
    parser.add_argument(
        '--apply',
        action='store_true',
        help='Send eligible chunk text to the configured embedding endpoint',
    )
    parser.add_argument('--batch-size', type=int, choices=range(1, 65), default=32, metavar='1..64')
    parser.add_argument(
        '--max-documents', type=int, choices=range(1, 101), default=20, metavar='1..100'
    )
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    settings = {
        name: env.get(name, '')
        for name in ('EMBEDDING_BASE_URL', 'EMBEDDING_API_KEY', 'EMBEDDING_MODEL')
    }
    engine = None
    try:
        engine = create_db_engine(args.database_url)
        result = asyncio.run(
            embed_pending(
                engine,
                LangChainProvider(settings=settings),
                apply=args.apply,
                batch_size=args.batch_size,
                max_documents=args.max_documents,
            )
        )
    except Exception:
        result = {'status': 'failed', 'error': '向量任务未完成；请检查数据库地址、迁移和配置。'}
    finally:
        if engine is not None:
            engine.dispose()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] in ('completed', 'dry_run') else 2


if __name__ == '__main__':
    raise SystemExit(main())
