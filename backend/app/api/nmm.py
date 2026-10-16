"""NMM HTTP transport: bounded inputs, origin checks and persisted progress streams."""

import asyncio
import json
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    ValidationError,
)
from starlette.datastructures import UploadFile

from ..nmm.billing import CostAuthorization
from ..nmm.providers import ImageInput
from ..nmm.schemas import NmmParams
from .evaluation import paid_origin

router = APIRouter(prefix='/api/v1/nmm')
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_JSON_BYTES = 32 * 1024
MAX_REFERENCES = 12
ACTIVE = frozenset(('generating', 'planning', 'rendering'))
RequestId = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=128)
]
Title = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=300)
]


class RunInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: RequestId
    revision: StrictInt = Field(ge=1)
    confirm_generation: StrictBool
    cost_authorization: CostAuthorization | None = None


class ConfirmInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: RequestId
    revision: StrictInt = Field(ge=1)
    action: Literal['select', 'regenerate']
    candidate_id: UUID | None = None
    params: NmmParams | None = None
    confirm_generation: StrictBool = False
    cost_authorization: CostAuthorization | None = None


class EstimateInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    params: NmmParams | None = None


class ProjectInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: Title
    params: NmmParams = Field(default_factory=NmmParams)


def json_request_docs(model):
    """Inline these nonrecursive input schemas; local $defs refs are not OAS components."""
    schema = model.model_json_schema()
    definitions = schema.pop('$defs', {})

    def inline(value):
        if isinstance(value, list):
            return [inline(item) for item in value]
        if isinstance(value, dict):
            if '$ref' in value:
                return inline(definitions[value['$ref'].removeprefix('#/$defs/')])
            return {key: inline(item) for key, item in value.items()}
        return value

    return {
        'requestBody': {
            'required': True,
            'content': {
                'application/json': {'schema': inline(schema)},
            },
        }
    }


PROJECT_REQUEST_DOCS = {
    'requestBody': {
        'required': True,
        'content': {
            'multipart/form-data': {
                'schema': {
                    'type': 'object',
                    'additionalProperties': False,
                    'required': ['title', 'original', 'mask'],
                    'properties': {
                        'title': {'type': 'string', 'minLength': 1, 'maxLength': 300},
                        'original': {
                            'type': 'string',
                            'format': 'binary',
                            'description': 'Opaque PNG/JPEG/WebP, at most 20 MiB and 4 MP.',
                        },
                        'mask': {
                            'type': 'string',
                            'format': 'binary',
                            'description': 'Same-sized binary mask: white edits, black preserves; at most 20 MiB.',
                        },
                        'params': {
                            'type': 'string',
                            'default': '{}',
                            'description': 'JSON object with metal, light_direction, contrast, step_count (4–9), notes. Unknown keys are rejected.',
                            'examples': ['{"metal":"silver","step_count":7}'],
                        },
                        'references': {
                            'type': 'array',
                            'maxItems': MAX_REFERENCES,
                            'items': {'type': 'string', 'format': 'binary'},
                            'description': 'Repeat this field for each image, at most 20 MiB each. references[] is also accepted.',
                        },
                        'roles': {
                            'type': 'array',
                            'maxItems': MAX_REFERENCES,
                            'items': {
                                'type': 'string',
                                'enum': [
                                    'reference_style',
                                    'reference_palette',
                                    'reference_steps',
                                ],
                            },
                            'description': 'One role per reference in the same order. Style/palette combined maximum 3. roles[] is also accepted.',
                        },
                    },
                }
            },
        },
    }
}


async def json_body(request, schema):
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_JSON_BYTES:
            raise HTTPException(413, 'NMM request exceeds 32 KiB')
        raw.extend(chunk)
    try:
        # JSON strict mode accepts UUID strings but never coerces booleans or integers.
        return schema.model_validate_json(bytes(raw), strict=True)
    except ValidationError:
        raise HTTPException(422, 'Invalid NMM request fields') from None


def one(form, name, default=None):
    values = form.getlist(name)
    if not values:
        return default
    if len(values) != 1:
        raise HTTPException(422, 'Duplicate NMM field')
    return values[0]


def repeated(form, name):
    plain, bracketed = form.getlist(name), form.getlist(name + '[]')
    if plain and bracketed:
        raise HTTPException(422, 'Do not mix multipart field aliases')
    return plain or bracketed


async def image_input(value):
    if not isinstance(value, UploadFile):
        raise HTTPException(422, 'NMM image upload is required')
    if value.size is not None and value.size > MAX_IMAGE_BYTES:
        raise HTTPException(413, 'NMM image exceeds 20 MiB')
    raw = bytearray()
    while chunk := await value.read(min(1024 * 1024, MAX_IMAGE_BYTES + 1 - len(raw))):
        raw.extend(chunk)
        if len(raw) > MAX_IMAGE_BYTES:
            raise HTTPException(413, 'NMM image exceeds 20 MiB')
    return ImageInput(bytes(raw), value.content_type or '')


async def multipart_input(request):
    if request.headers.get('content-type', '').split(';', 1)[0].strip() != 'multipart/form-data':
        raise HTTPException(415, 'NMM project creation requires multipart/form-data')
    total = 0
    # max_part_size only protects text fields. Bound the ASGI stream too, before
    # Starlette spools uploaded files; never trust Content-Length from the caller.
    limit = (MAX_REFERENCES + 2) * MAX_IMAGE_BYTES + 64 * 1024

    async def receive():
        nonlocal total
        message = await request.receive()
        if message['type'] == 'http.request':
            total += len(message.get('body', b''))
            if total > limit:
                raise HTTPException(413, 'NMM multipart request exceeds size limit')
        return message

    bounded = Request(request.scope, receive)
    async with bounded.form(
        max_files=MAX_REFERENCES + 2, max_fields=16, max_part_size=MAX_JSON_BYTES
    ) as form:
        allowed = {
            'title',
            'params',
            'original',
            'mask',
            'references',
            'references[]',
            'roles',
            'roles[]',
        }
        if set(form) - allowed:
            raise HTTPException(422, 'Unknown NMM multipart field')
        refs, roles = repeated(form, 'references'), repeated(form, 'roles')
        if len(refs) != len(roles) or len(refs) > MAX_REFERENCES:
            raise HTTPException(422, 'NMM reference images and roles must match (maximum 12)')
        if any(
            role not in ('reference_style', 'reference_palette', 'reference_steps')
            for role in roles
        ):
            raise HTTPException(422, 'Invalid NMM reference role')
        if sum(role != 'reference_steps' for role in roles) > 3:
            raise HTTPException(422, 'At most 3 style/palette references are allowed')
        try:
            raw_params = one(form, 'params', '{}')
            parsed = ProjectInput.model_validate(
                {
                    'title': one(form, 'title'),
                    'params': json.loads(raw_params),
                },
                strict=True,
            )
        except (ValueError, TypeError):
            raise HTTPException(422, 'Invalid NMM title or parameters') from None
        return {
            'title': parsed.title,
            'params': parsed.params,
            'original': await image_input(one(form, 'original')),
            'mask': await image_input(one(form, 'mask')),
            'references': [(role, await image_input(value)) for role, value in zip(roles, refs)],
        }


@router.post('/projects', status_code=201, openapi_extra=PROJECT_REQUEST_DOCS)
async def create_project(request: Request):
    paid_origin(request)
    values = await multipart_input(request)
    async with request.app.state.chat.lock:
        try:
            return request.app.state.nmm.create_project(**values)
        except ValueError:
            raise HTTPException(422, 'Invalid NMM input images or parameters') from None


@router.get('/projects')
def list_projects(request: Request):
    return request.app.state.nmm.list_projects()


@router.get('/projects/{project_id}')
def project_detail(project_id: UUID, request: Request):
    return request.app.state.nmm.detail(project_id)


@router.get('/projects/{project_id}/estimate')
def project_estimate(project_id: UUID, request: Request):
    return request.app.state.nmm.estimate(project_id)


@router.post('/projects/{project_id}/estimate', openapi_extra=json_request_docs(EstimateInput))
async def proposed_estimate(project_id: UUID, request: Request):
    paid_origin(request)
    data = await json_body(request, EstimateInput)
    async with request.app.state.chat.lock:
        return request.app.state.nmm.estimate(project_id, params=data.model_dump()['params'])


@router.post('/projects/{project_id}/run', openapi_extra=json_request_docs(RunInput))
async def run_project(project_id: UUID, request: Request):
    paid_origin(request)
    data = await json_body(request, RunInput)
    async with request.app.state.chat.lock:
        return await request.app.state.nmm.run(project_id, **data.model_dump())


@router.post('/projects/{project_id}/confirm', openapi_extra=json_request_docs(ConfirmInput))
async def confirm_project(project_id: UUID, request: Request):
    paid_origin(request)
    data = await json_body(request, ConfirmInput)
    async with request.app.state.chat.lock:
        return await request.app.state.nmm.confirm(project_id, **data.model_dump())


@router.post('/projects/{project_id}/stop')
async def stop_project(project_id: UUID, request: Request):
    paid_origin(request)
    async with request.app.state.chat.lock:
        return await request.app.state.nmm.stop(project_id)


@router.delete('/projects/{project_id}', status_code=204)
async def delete_project(project_id: UUID, request: Request):
    paid_origin(request)
    async with request.app.state.chat.lock:
        await request.app.state.nmm.delete_project(project_id)
    return Response(status_code=204)


@router.get('/assets/{asset_id}')
def read_asset(asset_id: UUID, request: Request):
    raw, content_type = request.app.state.nmm.read_asset(asset_id)
    return Response(
        raw,
        media_type=content_type,
        headers={
            'X-Content-Type-Options': 'nosniff',
            'Cache-Control': 'no-store',
        },
    )


@router.get('/projects/{project_id}/events')
async def project_events(project_id: UUID, request: Request):
    service = request.app.state.nmm
    service.detail(project_id)  # Resolve 404 before streaming headers are sent.

    async def stream():
        previous = None
        while not await request.is_disconnected():
            try:
                detail = service.detail(project_id)
                encoded = json.dumps(detail, ensure_ascii=False)
            except Exception:
                # Status reads may fail or the project may be deleted mid-stream.
                # Never send database paths, credentials or exception text over SSE.
                yield 'event: error\ndata: {"detail":"NMM progress unavailable"}\n\n'
                return
            if encoded != previous:
                progress = {
                    key: detail.get(key) for key in ('status', 'phase', 'revision', 'active_job_id')
                }
                yield 'event: progress\ndata: ' + json.dumps(progress) + '\n\n'
                yield 'event: snapshot\ndata: ' + encoded + '\n\n'
                previous = encoded
            else:
                yield ': keep-alive\n\n'
            if detail['status'] not in ACTIVE:
                return
            await asyncio.sleep(0.2)

    return StreamingResponse(
        stream(),
        media_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        },
    )
