"""Explicit starts and durable progress for ingestion and model work."""
from typing import Literal
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from ..runtime_config import loopback_endpoint

router = APIRouter(prefix='/api/v1/jobs')


class IngestionTask(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['embedding', 'vision']
    request_id: str = Field(min_length=1, max_length=128, pattern=r'^\S+$')
    max_documents: StrictInt = Field(default=20, ge=1, le=100)
    apply: Literal[True]


def check_origin(request):
    origin = request.headers.get('origin')
    same = origin == str(request.base_url).rstrip('/')
    dev = {f'http://{host}:{port}' for host in ('localhost', '127.0.0.1', '[::1]') for port in (5173, 5174)}
    if origin and not (same or origin in dev and loopback_endpoint(str(request.base_url))):
        raise HTTPException(403, '不允许跨来源启动或取消任务。')


@router.get('')
async def history(request: Request):
    return request.app.state.jobs.history()


@router.get('/{id}')
async def detail(id: int, request: Request):
    return request.app.state.jobs.snapshot(id)


@router.post('/{id}/stop')
async def stop(id: int, request: Request):
    check_origin(request)
    return await request.app.state.jobs.cancel(id)


@router.post('', status_code=202)
async def start(data: IngestionTask, request: Request):
    check_origin(request)
    state = request.app.state
    provider = state.chat.provider
    if not getattr(provider, f'{data.kind}_configured', False):
        raise HTTPException(503, '尚未配置该任务所需的模型。')
    # Take the selected provider snapshot; configuration changes cannot redirect an active task.
    async def execute(context):
        if data.kind == 'embedding':
            from ..embedding_jobs import embed_pending
            result = await embed_pending(state.engine, provider, apply=True, max_documents=data.max_documents, progress=context.progress)
        else:
            from ..vision_jobs import describe_pending
            from ..services.ocr import TesseractOCR
            result = await describe_pending(state.engine, provider, state.storage,
                                            apply=True, max_documents=data.max_documents, progress=context.progress,
                                            ocr=TesseractOCR(tessdata_dir=getattr(provider, 'settings', {}).get('OCR_TESSDATA_DIR')))
        documents = result.get('documents', [])
        context.progress(len(documents), len(documents))
        if any(item.get('status') == 'failed' for item in documents):
            result['status'] = 'failed'
        return result
    async with state.chat.lock:
        return await state.jobs.submit(data.kind, data.request_id, data.model_dump(), execute)


def install_jobs(app):
    app.include_router(router)
