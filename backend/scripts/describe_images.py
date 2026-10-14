"""Explicit local OCR + vision CLI. The default dry run never invokes a model."""
import argparse
import asyncio
import json
import os

from app.db import create_db_engine, init_db
from app.llm import LangChainProvider
from app.services.ocr import TesseractOCR
from app.vision_jobs import describe_pending


def main(argv=None, *, environ=None):
    parser = argparse.ArgumentParser(description='Plan image interpretation; --apply authorizes image model requests.')
    parser.add_argument('--database-url', required=True, help='Explicit target database with current Alembic schema')
    parser.add_argument('--storage-dir', required=True, help='Explicit attachment storage directory')
    parser.add_argument('--apply', action='store_true', help='Run local OCR and send eligible images to the configured vision endpoint')
    parser.add_argument('--max-documents', type=int, choices=range(1, 101), default=20, metavar='1..100',
                        help='Maximum image targets, including their linked documents')
    parser.add_argument('--tessdata-dir', help='Local Tesseract language data directory')
    parser.add_argument('--languages', default='chi_sim+eng', help='Installed Tesseract languages')
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    settings = {key: env.get(key, '') for key in ('VISION_BASE_URL', 'VISION_API_KEY', 'VISION_MODEL')}
    engine = None
    try:
        engine = create_db_engine(args.database_url)
        init_db(engine)
        result = asyncio.run(describe_pending(engine, LangChainProvider(settings=settings), args.storage_dir,
            apply=args.apply, max_documents=args.max_documents,
            ocr=TesseractOCR(languages=args.languages, tessdata_dir=args.tessdata_dir)))
    except Exception:
        result = {'status': 'failed', 'error': '图片任务未完成；请检查数据库迁移、附件目录及模型和 OCR 配置。'}
    finally:
        if engine is not None:
            engine.dispose()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] in ('completed', 'dry_run') else 2


if __name__ == '__main__':
    raise SystemExit(main())
