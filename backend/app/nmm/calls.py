"""One explicit network attempt per durable reservation; no implicit retry."""

import asyncio
from dataclasses import asdict
from hashlib import sha256
from time import perf_counter

from pydantic import ValidationError

from app.ingestion import digest
from .provider_types import CallUsage, NmmProviderError, ProviderResult


def error_record(error):
    result = {'code': error.code, 'message': str(error), 'retryable': error.retryable}
    for name in ('timeout_phase', 'request_bytes'):
        value = getattr(error, name, None)
        if value is not None:
            result[name] = value
    return result


async def invoke(operations, kind, key, images, prompt, *, schema=None, mask=None, references=()):
    service = operations.service
    provider = service.vision if kind == 'vision' else service.image_edit
    live = getattr(provider, 'mode', None) == 'live'
    call_id = None
    started = perf_counter()
    image_count = len(images) + len(references)
    if live:
        payload = {
            'images': [sha256(image.data).hexdigest() for image in images],
            'references': [sha256(image.data).hexdigest() for image in references],
            'mask': sha256(mask.data).hexdigest() if mask else None,
            'prompt': prompt,
            'schema': schema.model_json_schema() if schema else None,
        }
        call_id, cached = service.billing.reserve(
            operations.project_id,
            revision=operations.revision,
            thread=operations.thread,
            kind=kind,
            logical_key=key,
            fingerprint=digest(payload),
            guard=operations.guard,
            usage={**_unknown_usage(provider, started, image_count), 'elapsed_ms': None},
        )
        if cached is not None:
            return schema.model_validate(cached)
    usage = None
    try:
        response = (
            await provider.analyze(images, prompt, schema)
            if kind == 'vision'
            else await provider.edit(images[0], mask, list(references), prompt, 1)
        )
        value = response.value if isinstance(response, ProviderResult) else response
        usage = asdict(response.usage) if isinstance(response, ProviderResult) else None
        if live and not isinstance(response, ProviderResult):
            raise NmmProviderError('invalid_output', '真实模型未提供可核对的调用结果。')
        if kind == 'vision':
            try:
                value = schema.model_validate(value)
            except (ValidationError, ValueError, TypeError):
                raise NmmProviderError(
                    'invalid_output', '模型返回的结构化内容不符合要求。'
                ) from None
        if live:
            service.billing.finish(
                operations.project_id,
                call_id,
                status='completed',
                result=value.model_dump() if kind == 'vision' else None,
                usage=usage,
            )
        return value
    except asyncio.CancelledError:
        if live:
            service.billing.finish(
                operations.project_id,
                call_id,
                status='unknown',
                usage=usage or _unknown_usage(provider, started, image_count),
                error={
                    'code': 'network',
                    'message': '调用已中断，服务端结果未知。',
                    'retryable': True,
                },
            )
        raise
    except NmmProviderError as error:
        if live:
            service.billing.finish(
                operations.project_id,
                call_id,
                status='unknown' if error.outcome_unknown else 'failed',
                usage=asdict(error.usage)
                if error.usage
                else usage or _unknown_usage(provider, started, image_count),
                error=error_record(error),
            )
        raise
    except Exception:
        if live:
            service.billing.finish(
                operations.project_id,
                call_id,
                status='unknown',
                usage=usage or _unknown_usage(provider, started, image_count),
                error={
                    'code': 'network',
                    'message': '调用结果无法确认，请重新授权后重试。',
                    'retryable': True,
                },
            )
        raise


def _unknown_usage(provider, started, image_count):
    public = getattr(provider, 'public_config', {})
    return asdict(
        CallUsage(
            provider=public.get('provider', 'unknown'),
            model=public.get('model', 'unknown'),
            input_images=image_count,
            elapsed_ms=round((perf_counter() - started) * 1000),
        )
    )
