"""Evaluation HTTP parsing and routing; no persistence or ranking rules."""

import asyncio
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import ValidationError
from ..evaluation_schemas import DatasetInput, RunInput, PreviewInput
from ..services.evaluation import EvaluationService
from .jobs import check_origin

router = APIRouter(prefix='/api/v1')
MAX_REQUEST = 1024 * 1024


async def body(request, model):
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_REQUEST:
            raise HTTPException(413, 'Evaluation request exceeds 1 MiB')
        raw.extend(chunk)
    try:
        return model.model_validate_json(bytes(raw))
    except ValidationError as exc:
        raise HTTPException(422, str(exc)) from None


def paid_origin(request):
    check_origin(request)
    if request.headers.get('sec-fetch-site') == 'cross-site':
        raise HTTPException(403, '不允许跨来源调用检索模型。')


@router.get('/retrieval/preview')
def preview_legacy(
    request: Request,
    q: str = Query(default='', max_length=2000),
    k: int = Query(default=5, ge=1, le=20),
):
    return request.app.state.evaluation.preview_legacy(q, k)


@router.post('/retrieval/preview')
async def preview(request: Request):
    paid_origin(request)
    data = await body(request, PreviewInput)
    return await request.app.state.evaluation.preview(data.query, data.k)


@router.get('/evaluations/datasets')
def datasets(request: Request):
    return request.app.state.evaluation.datasets()


@router.get('/evaluations/datasets/{id}')
def get_dataset(id: str, request: Request):
    return request.app.state.evaluation.dataset(id)


@router.post('/evaluations/datasets', status_code=201)
async def create_dataset(request: Request):
    data = await body(request, DatasetInput)
    return await asyncio.to_thread(request.app.state.evaluation.save_dataset, data)


@router.put('/evaluations/datasets/{id}')
async def update_dataset(id: str, request: Request):
    data = await body(request, DatasetInput)
    return await asyncio.to_thread(request.app.state.evaluation.save_dataset, data, id)


@router.delete('/evaluations/datasets/{id}')
def delete_dataset(id: str, request: Request):
    return request.app.state.evaluation.delete_dataset(id)


@router.post('/evaluations/runs')
async def run(request: Request):
    data = await body(request, RunInput)
    if data.strategies is not None:
        paid_origin(request)
    return await request.app.state.evaluation.run(data)


@router.get('/evaluations/runs')
def history(request: Request):
    return request.app.state.evaluation.reports()


@router.get('/evaluations/runs/{id}')
def report(id: int, request: Request):
    return request.app.state.evaluation.report(id)


def install_evaluation(app, engine):
    app.state.evaluation = EvaluationService(engine, app.state.retrieval)
    app.include_router(router)
