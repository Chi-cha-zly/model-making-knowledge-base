"""Settings HTTP origin and secret-safe request validation."""

import json
from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError
from ..runtime_config import loopback_endpoint
from ..settings_schemas import ProfileInput, PromptInput
from ..services.settings import SettingsService


async def parse(request, model=None):
    origin = request.headers.get('origin')
    # Vite forwards the browser Origin while the backend sees the proxy Host.
    # Only the documented local UI ports are trusted, and only on a local backend.
    dev_origins = {
        f'http://{host}:{port}'
        for host in ('localhost', '127.0.0.1', '[::1]')
        for port in (5173, 5174)
    }
    same_origin = origin == str(request.base_url).rstrip('/')
    trusted_dev = origin in dev_origins and loopback_endpoint(str(request.base_url))
    if origin and not (same_origin or trusted_dev):
        raise HTTPException(403, '不允许跨来源修改设置。')
    if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
        raise HTTPException(415, '请使用 application/json。')
    try:
        # Bound before parsing: errors must never include request bodies or secret input.
        raw = await request.body()
        if len(raw) > 32768:
            raise ValueError()
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError()
        return model.model_validate(body) if model else body
    except (ValueError, ValidationError):
        raise HTTPException(422, '设置字段无效；请检查地址、必填字段和长度限制。') from None


def install_settings(app, store, chat, experiments):
    service = SettingsService(store, chat, experiments)
    router = APIRouter(prefix='/api/v1/settings')

    @router.get('')
    async def get_settings():
        return store.view()

    @router.post('/profiles')
    async def create(request: Request):
        return await service.create(await parse(request, ProfileInput))

    @router.put('/profiles/{id}')
    async def update(id: str, request: Request):
        return await service.update(id, await parse(request, ProfileInput))

    @router.delete('/profiles/{id}')
    async def delete(id: str, request: Request):
        await parse(request)
        return await service.delete(id)

    @router.post('/profiles/{id}/activate')
    async def activate(id: str, request: Request):
        await parse(request)
        return await service.activate(id)

    @router.put('/prompt')
    async def prompt(request: Request):
        return await service.prompt(await parse(request, PromptInput))

    @router.post('/profiles/{id}/test')
    async def test(id: str, request: Request):
        await parse(request)
        return await service.test(id)

    app.state.user_settings = store
    app.state.settings_service = service
    app.include_router(router)
