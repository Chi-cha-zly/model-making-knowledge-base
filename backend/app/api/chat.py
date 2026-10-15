"""Chat HTTP routes and streaming representation."""

import asyncio
import json
from fastapi import APIRouter
from fastapi.responses import Response, StreamingResponse
from ..chat_schemas import TitleInput, RequestInput, TurnInput
from ..repositories.chat import ACTIVE
from ..services.errors import ServiceError


def install_chat(app, service):
    router = APIRouter(prefix='/api/v1')
    app.state.chat = service

    @router.get('/agent/config')
    async def config():
        configured = service.provider.configured
        return {
            'configured': configured,
            'model': service.provider.model,
            'message': '模型已配置（连接状态将在生成时检查）。'
            if configured
            else '请在项目根目录 .env 中配置 AI_BASE_URL、AI_MODEL 和 AI_API_KEY；本地服务可省略密钥。',
        }

    @router.get('/conversations')
    async def conversations():
        return service.conversations()

    @router.post('/conversations', status_code=201)
    async def create_conversation(data: TitleInput):
        async with service.lock:
            return service.create_conversation(data.title)

    @router.get('/conversations/{cid}')
    async def detail(cid: int):
        return service.detail(cid)

    @router.patch('/conversations/{cid}')
    async def rename(cid: int, data: TitleInput):
        async with service.lock:
            return service.rename(cid, data.title)

    @router.delete('/conversations/{cid}', status_code=204)
    async def remove(cid: int):
        async with service.lock:
            await service.remove(cid)
        return Response(status_code=204)

    @router.post('/conversations/{cid}/runs', status_code=201)
    async def start(cid: int, data: TurnInput):
        async with service.lock:
            return await service.create_run(cid, data)

    @router.get('/runs/{rid}')
    async def run(rid: int):
        return service.snapshot(rid)

    @router.get('/runs/{rid}/events')
    async def events(rid: int):
        service.snapshot(rid)

        async def stream():
            previous = None
            while True:
                try:
                    snapshot = service.snapshot(rid)
                except ServiceError:
                    return
                data = json.dumps(snapshot, ensure_ascii=False)
                if data != previous:
                    yield f'event: snapshot\ndata: {data}\n\n'
                    previous = data
                else:
                    yield ': keepalive\n\n'
                if snapshot['status'] not in ACTIVE:
                    return
                await asyncio.sleep(0.15)

        return StreamingResponse(
            stream(),
            media_type='text/event-stream',
            headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
        )

    @router.post('/runs/{rid}/stop')
    async def stop(rid: int):
        async with service.lock:
            return await service.stop(rid)

    @router.post('/runs/{rid}/retry', status_code=201)
    async def retry(rid: int, data: RequestInput):
        async with service.lock:
            return await service.create_run(
                service.snapshot(rid)['conversation_id'], data, 'retry', rid
            )

    @router.post('/runs/{rid}/steer', status_code=201)
    async def steer(rid: int, data: TurnInput):
        async with service.lock:
            return await service.create_run(
                service.snapshot(rid)['conversation_id'], data, 'steer', rid
            )

    app.include_router(router)
