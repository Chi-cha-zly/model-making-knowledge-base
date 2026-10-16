import asyncio
from dataclasses import fields

import httpx
import pytest
from pydantic import BaseModel

from app.nmm.provider_config import build_nmm_providers
from app.nmm.provider_types import CallUsage, NmmProviderError, ProviderResult
from nmm_fakes import make_inputs, make_png


class Output(BaseModel):
    summary: str


def test_public_types_contract():
    assert [field.name for field in fields(ProviderResult)] == ['value', 'usage']
    usage = CallUsage('test', 'model')
    assert usage.estimated_cost is None and usage.input_tokens is None
    error = NmmProviderError('timeout', '请求超时。', usage=usage, outcome_unknown=True)
    assert error.usage is usage and error.outcome_unknown


def test_no_ambient_configuration_or_network(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'ambient-not-allowed')
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://unexpected.invalid/v1')

    def fail(request):
        pytest.fail('factory must never issue network requests')

    vision, image = build_nmm_providers(
        {'AI_API_KEY': 'chat', 'VISION_API_KEY': 'vision'}, transport=httpx.MockTransport(fail)
    )
    assert getattr(image, 'mode', 'unconfigured') == 'unconfigured'
    assert image.safe_message
    assert getattr(vision, 'mode', 'unconfigured') == 'unconfigured'


def settings():
    return {
        'IMAGE_BASE_URL': 'https://api.openai.com/v1',
        'IMAGE_MODEL': 'gpt-image-2.5-sunburst',
        'IMAGE_API_KEY': 'image-secret',
        'VISION_BASE_URL': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
        'VISION_MODEL': 'qwen3-vl-plus',
        'VISION_API_KEY': 'vision-secret',
    }


def prompt(task='analysis', notes='user-private-note'):
    import json

    return json.dumps(
        {'task': task, 'instruction': 'Fixed safe task instruction.', 'params': {'notes': notes}}
    )


def image_response(*, raw=None, **kwargs):
    import base64

    return {
        'data': [
            {
                'b64_json': base64.b64encode(
                    raw if raw is not None else make_png(size=(1024, 1024))
                ).decode()
            }
        ],
        **kwargs,
    }


def vision_response(content='{"summary":"valid"}', **kwargs):
    return {'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}], **kwargs}


def run_image(handler, **kwargs):
    _, image = build_nmm_providers(settings(), transport=httpx.MockTransport(handler))
    return asyncio.run(
        image.edit(
            image=make_inputs()['original'],
            mask=make_inputs()['mask'],
            references=[],
            prompt=prompt('generate'),
            n=1,
            **kwargs,
        )
    )


def test_image_multipart_source_first_alpha_resize_no_fidelity_and_usage():
    from email import policy
    from email.parser import BytesParser
    from io import BytesIO
    from PIL import Image
    from app.nmm.providers import ImageInput

    source = ImageInput(make_png(size=(1600, 800)), 'image/png')
    selection = Image.new('RGB', (1600, 800), 'black')
    selection.paste('white', (0, 0, 800, 800))
    stream = BytesIO()
    selection.save(stream, 'PNG')
    mask = ImageInput(stream.getvalue(), 'image/png')
    references = [ImageInput(make_png((0, 255, 0)), 'image/png')]

    def handler(request):
        assert str(request.url) == 'https://api.openai.com/v1/images/edits'
        assert request.headers['authorization'] == 'Bearer image-secret'
        assert request.extensions['timeout']['connect'] == 15
        message = BytesParser(policy=policy.default).parsebytes(
            ('Content-Type: ' + request.headers['content-type'] + '\r\n\r\n').encode()
            + request.read()
        )
        parts = [
            (part.get_param('name', header='content-disposition'), part.get_payload(decode=True))
            for part in message.iter_parts()
        ]
        values = {name: raw for name, raw in parts if name != 'image[]'}
        assert (
            values['n'] == b'1'
            and values['background'] == b'opaque'
            and values['output_format'] == b'png'
        )
        assert values['quality'] == b'medium' and values['size'] == b'1536x1024'
        assert 'input_fidelity' not in values and 'response_format' not in values
        image_parts = [raw for name, raw in parts if name == 'image[]']
        assert len(image_parts) == 2
        with (
            Image.open(BytesIO(image_parts[0])) as first,
            Image.open(BytesIO(image_parts[1])) as second,
            Image.open(BytesIO(values['mask'])) as alpha,
        ):
            assert first.size == alpha.size == (1536, 1024)
            assert second.getpixel((0, 0))[:3] == (0, 255, 0)
            assert alpha.getpixel((0, 128))[3] == 0
            assert alpha.getpixel((1535, 128))[3] == 255
            assert alpha.getpixel((0, 0))[3] == alpha.getpixel((0, 1023))[3] == 255
            assert set(alpha.getchannel('A').get_flattened_data()) == {0, 255}
        usage = {
            'input_tokens': 30,
            'output_tokens': 40,
            'total_tokens': 70,
            'input_tokens_details': {'text_tokens': 10, 'image_tokens': 20},
        }
        return httpx.Response(
            200,
            json=image_response(raw=image_parts[0], usage=usage),
            headers={'x-request-id': 'req-safe'},
        )

    _, provider = build_nmm_providers(settings(), transport=httpx.MockTransport(handler))
    result = asyncio.run(provider.edit(source, mask, references, prompt('generate'), 1))
    assert (
        len(result.value) == 1
        and result.usage.input_images == 2
        and result.usage.output_images == 1
    )
    assert result.usage.request_id == 'req-safe' and result.usage.estimated_cost == pytest.approx(
        (10 * 5 + 20 * 8 + 40 * 30) / 1e6
    )
    with Image.open(BytesIO(result.value[0])) as restored:
        assert restored.size == (1600, 800)
        assert restored.getpixel((0, 0))[:3] == (80, 90, 100)


def test_vision_multi_image_json_mode_schema_and_user_notes_isolation():
    import base64
    import json
    from io import BytesIO
    from PIL import Image

    def handler(request):
        assert request.headers['content-type'] == 'application/json'
        assert request.headers['authorization'] == 'Bearer vision-secret'
        data = json.loads(request.content)
        assert data['enable_thinking'] is False and data['response_format'] == {
            'type': 'json_object'
        }
        assert data['stream'] is False
        system, user = data['messages']
        assert (
            system['role'] == 'system'
            and 'JSON' in system['content']
            and 'summary' in system['content']
        )
        assert (
            'user-private-note' not in system['content']
            and 'user-private-note' in user['content'][0]['text']
        )
        urls = [part['image_url']['url'] for part in user['content'] if part['type'] == 'image_url']
        assert len(urls) == 2
        for url in urls:
            assert url.startswith('data:image/png;base64,')
            with Image.open(BytesIO(base64.b64decode(url.split(',')[1]))) as decoded:
                assert max(decoded.size) <= 768
        return httpx.Response(
            200,
            json=vision_response(
                usage={'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}
            ),
            headers={'x-request-id': 'vision-req'},
        )

    vision, _ = build_nmm_providers(settings(), transport=httpx.MockTransport(handler))
    images = [make_inputs()['original']] * 2
    result = asyncio.run(vision.analyze(images, prompt('review'), Output))
    assert result.value.summary == 'valid' and result.usage.input_images == 2
    assert result.usage.estimated_cost == pytest.approx((100 * 0.143 + 20 * 1.434) / 1e6)


@pytest.mark.parametrize(
    'status,code',
    [
        (401, 'auth'),
        (403, 'auth'),
        (429, 'rate_limited'),
        (500, 'provider_error'),
        (302, 'configuration'),
        (408, 'timeout'),
    ],
)
def test_http_errors_are_single_request_safe_and_keep_usage(status, code):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(
            status,
            json={
                'error': {'message': 'image-secret /private/path'},
                'usage': {'input_tokens': 10},
            },
            headers={'Location': 'https://evil.invalid', 'x-request-id': 'safe-id'},
        )

    with pytest.raises(NmmProviderError) as failure:
        run_image(handler)
    error = failure.value
    assert error.code == code and len(calls) == 1
    assert 'secret' not in str(error) and '/private' not in str(error)
    assert error.usage.input_tokens == 10 and error.usage.request_id == 'safe-id'


@pytest.mark.parametrize(
    'exception,code', [(httpx.ReadTimeout, 'timeout'), (httpx.ConnectError, 'network')]
)
def test_network_errors_never_retry_and_are_unknown(exception, code):
    calls = []

    def handler(request):
        calls.append(request)
        raise exception('secret', request=request)

    with pytest.raises(NmmProviderError) as failure:
        run_image(handler)
    assert len(calls) == 1 and failure.value.code == code and failure.value.outcome_unknown
    assert failure.value.usage.estimated_cost is None


@pytest.mark.parametrize(
    'payload',
    [
        {'data': [{'url': 'https://evil.invalid/image.png'}]},
        {'data': [{'b64_json': '!notbase64!'}]},
        {'data': []},
        {'data': [{'b64_json': 'eA=='}]},
        {'data': [{'b64_json': 'eA=='}] * 2},
    ],
)
def test_invalid_image_outputs_are_not_downloaded_or_retried(payload):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=payload)

    with pytest.raises(NmmProviderError) as failure:
        run_image(handler)
    assert failure.value.code == 'invalid_output' and len(calls) == 1


@pytest.mark.parametrize(
    'content,finish', [('not-json', 'stop'), ('{}', 'stop'), ('{"summary":"truncated"}', 'length')]
)
def test_vision_schema_and_truncation_errors_keep_usage(content, finish):
    def handler(request):
        result = vision_response(
            content, usage={'prompt_tokens': 11, 'completion_tokens': 7, 'total_tokens': 18}
        )
        result['choices'][0]['finish_reason'] = finish
        return httpx.Response(200, json=result)

    vision, _ = build_nmm_providers(settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(NmmProviderError) as failure:
        asyncio.run(vision.analyze([make_inputs()['original']], prompt(), Output))
    assert failure.value.code == 'invalid_output' and failure.value.usage.total_tokens == 18


def test_configuration_ids_track_credentials_without_exposing_them():
    first = settings()
    vision, image = build_nmm_providers(first)
    changed = dict(first, IMAGE_API_KEY='new-secret')
    vision2, image2 = build_nmm_providers(changed)
    assert image.configuration_id != image2.configuration_id
    assert vision.configuration_id == vision2.configuration_id
    assert image.mode == vision.mode == 'live'
    assert all('secret' not in str(value) for value in (image.public_config, vision.public_config))


@pytest.mark.parametrize(
    'field,value',
    [
        ('IMAGE_BASE_URL', 'http://api.openai.com/v1'),
        ('IMAGE_BASE_URL', 'https://api.openai.com.evil.invalid/v1'),
        ('IMAGE_BASE_URL', 'https://api.openai.com/v1?key=secret'),
        ('IMAGE_BASE_URL', 'https://user@api.openai.com/v1'),
        ('IMAGE_MODEL', 'gpt-image-1.5'),
        ('VISION_MODEL', 'unsupported'),
        ('VISION_BASE_URL', 'https://unknown.aliyuncs.com/compatible-mode/v1'),
    ],
)
def test_unsupported_configuration_is_fail_closed(field, value):
    vision, image = build_nmm_providers(dict(settings(), **{field: value}))
    assert (image if field.startswith('IMAGE') else vision).mode == 'unconfigured'


def test_missing_usage_remains_unknown_and_output_count_is_per_call():
    result = run_image(lambda request: httpx.Response(200, json=image_response()))
    assert (
        result.usage.input_tokens is None
        and result.usage.estimated_cost is None
        and result.usage.currency is None
    )
    assert result.usage.output_images == 1


def test_encoded_multipart_limit_is_checked_before_network(monkeypatch):
    monkeypatch.setattr('app.nmm.live_providers.MAX_BODY_BYTES', 100)

    def fail(request):
        pytest.fail('oversized complete request must never be sent')

    with pytest.raises(NmmProviderError) as failure:
        run_image(fail)
    assert failure.value.code == 'configuration'


def test_response_stream_and_content_length_limits():
    _, image = build_nmm_providers(
        settings(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=b'x' * 100, headers={'content-length': '100'}
            )
        ),
    )
    image.max_response_bytes = 64
    with pytest.raises(NmmProviderError) as failure:
        asyncio.run(
            image.edit(make_inputs()['original'], make_inputs()['mask'], [], prompt('generate'), 1)
        )
    assert failure.value.code == 'invalid_output'

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(3):
                yield b'x' * 32

    _, image = build_nmm_providers(
        settings(),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream())),
    )
    image.max_response_bytes = 64
    with pytest.raises(NmmProviderError):
        asyncio.run(
            image.edit(make_inputs()['original'], make_inputs()['mask'], [], prompt('generate'), 1)
        )


@pytest.mark.parametrize('code', ['moderation_blocked', 'DataInspectionFailed'])
def test_provider_moderation_errors_are_classified_without_raw_text(code):
    with pytest.raises(NmmProviderError) as failure:
        run_image(
            lambda request: httpx.Response(
                400, json={'error': {'code': code, 'message': 'private prompt'}}
            )
        )
    assert failure.value.code == 'content_filtered' and 'private' not in failure.value.message


@pytest.mark.parametrize('case', ['references', 'n', 'mask', 'prompt'])
def test_edit_input_protocol_errors_do_not_issue_http(case):
    def fail(request):
        pytest.fail('invalid input must not issue HTTP')

    inputs = make_inputs()
    refs, count, text = [], 1, prompt('generate')
    if case == 'references':
        refs = [inputs['original']] * 4
    if case == 'n':
        count = 2
    if case == 'mask':
        inputs['mask'] = inputs['original']
    if case == 'prompt':
        text = 'not a JSON envelope'
    _, image = build_nmm_providers(settings(), transport=httpx.MockTransport(fail))
    with pytest.raises(NmmProviderError) as failure:
        asyncio.run(image.edit(inputs['original'], inputs['mask'], refs, text, count))
    assert failure.value.code == 'configuration'


def test_cancelled_request_is_not_retried_and_preserves_async_cancellation():
    async def exercise():
        calls = []
        started = asyncio.Event()

        async def handler(request):
            calls.append(request)
            started.set()
            await asyncio.Event().wait()

        _, image = build_nmm_providers(settings(), transport=httpx.MockTransport(handler))
        task = asyncio.create_task(
            image.edit(make_inputs()['original'], make_inputs()['mask'], [], prompt('generate'), 1)
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(calls) == 1

    asyncio.run(exercise())


@pytest.mark.parametrize(
    'base,region',
    [
        ('https://dashscope-intl.aliyuncs.com/compatible-mode/v1', 'singapore'),
        ('https://workspace-1.cn-beijing.maas.aliyuncs.com/compatible-mode/v1', 'beijing'),
        ('https://workspace-1.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1', 'singapore'),
    ],
)
def test_explicit_dashscope_regions_and_pricing(base, region):
    config = dict(settings(), VISION_BASE_URL=base)
    vision, _ = build_nmm_providers(
        config,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=vision_response(
                    usage={'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}
                ),
            )
        ),
    )
    result = asyncio.run(vision.analyze([make_inputs()['original']], prompt(), Output))
    assert vision.public_config['region'] == region
    rate_in, rate_out = (0.143, 1.434) if region == 'beijing' else (0.2, 1.6)
    assert result.usage.estimated_cost == pytest.approx((100 * rate_in + 20 * rate_out) / 1e6)


@pytest.mark.parametrize(
    'payload',
    [
        {'choices': ['not-an-object']},
        {'choices': [{'finish_reason': 'stop', 'message': None}]},
        {'choices': [None]},
    ],
)
def test_malformed_vision_response_always_becomes_safe_typed_error(payload):
    vision, _ = build_nmm_providers(
        settings(), transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    )
    with pytest.raises(NmmProviderError) as failure:
        asyncio.run(vision.analyze([make_inputs()['original']], prompt(), Output))
    assert failure.value.code == 'invalid_output'


def test_default_network_client_disables_environment_redirects_and_retries(monkeypatch):
    import app.nmm.live_providers as module

    client_options, transport_options = [], []
    original_client = httpx.AsyncClient

    def transport(**kwargs):
        transport_options.append(kwargs)
        return httpx.MockTransport(lambda request: httpx.Response(200, json=image_response()))

    def client(**kwargs):
        client_options.append(kwargs)
        return original_client(**kwargs)

    monkeypatch.setattr(module.httpx, 'AsyncHTTPTransport', transport)
    monkeypatch.setattr(module.httpx, 'AsyncClient', client)
    _, image = build_nmm_providers(settings())
    asyncio.run(
        image.edit(make_inputs()['original'], make_inputs()['mask'], [], prompt('generate'), 1)
    )
    assert transport_options == [{'retries': 0, 'trust_env': False}]
    assert (
        client_options[0]['follow_redirects'] is False and client_options[0]['trust_env'] is False
    )


@pytest.mark.parametrize('capability', ['image', 'vision'])
def test_existing_large_reference_contract_is_preserved_then_downscaled(capability):
    import base64
    from email import policy
    from email.parser import BytesParser
    from io import BytesIO
    import json
    from PIL import Image
    from app.nmm.providers import ImageInput

    large_reference = ImageInput(make_png(size=(2001, 2000)), 'image/png')

    def handler(request):
        if capability == 'vision':
            body = json.loads(request.content)
            url = body['messages'][1]['content'][2]['image_url']['url']
            raw = base64.b64decode(url.split(',')[1])
            response = vision_response()
        else:
            message = BytesParser(policy=policy.default).parsebytes(
                ('Content-Type: ' + request.headers['content-type'] + '\r\n\r\n').encode()
                + request.read()
            )
            images = [
                part.get_payload(decode=True)
                for part in message.iter_parts()
                if part.get_param('name', header='content-disposition') == 'image[]'
            ]
            raw = images[1]
            response = image_response()
        with Image.open(BytesIO(raw)) as decoded:
            assert max(decoded.size) <= 1536 and decoded.width * decoded.height < 4_000_000
        return httpx.Response(200, json=response)

    vision, image = build_nmm_providers(settings(), transport=httpx.MockTransport(handler))
    if capability == 'vision':
        result = asyncio.run(
            vision.analyze([make_inputs()['original'], large_reference], prompt(), Output)
        )
    else:
        result = asyncio.run(
            image.edit(
                make_inputs()['original'],
                make_inputs()['mask'],
                [large_reference],
                prompt('generate'),
                1,
            )
        )
    assert result.usage.input_images == 2


@pytest.mark.parametrize('capability,label', [('vision', '视觉'), ('image', '图像编辑')])
@pytest.mark.parametrize('phase', ['connect', 'write', 'read', 'pool', 'overall', 'http_408'])
def test_timeout_phase_is_safe_persistable_and_never_retried(capability, label, phase):
    from app.nmm.calls import error_record

    calls = []

    async def handler(request):
        calls.append(request)
        if phase == 'http_408':
            return httpx.Response(408, json={'error': {'message': 'private-secret'}})
        if phase == 'overall':
            await asyncio.sleep(1)
        exception = {
            'connect': httpx.ConnectTimeout,
            'write': httpx.WriteTimeout,
            'read': httpx.ReadTimeout,
            'pool': httpx.PoolTimeout,
        }[phase]
        raise exception('private-secret', request=request)

    vision, image = build_nmm_providers(settings(), transport=httpx.MockTransport(handler))
    provider = vision if capability == 'vision' else image
    if phase == 'overall':
        provider.timeout_seconds = 0.01
    with pytest.raises(NmmProviderError) as caught:
        asyncio.run(provider.request('/test', 2, content=b'{}'))
    error = caught.value
    assert error.timeout_phase == phase
    assert label in str(error) and 'private-secret' not in str(error)
    assert error.code == 'timeout' and error.outcome_unknown and error.retryable
    record = error_record(error)
    assert record['timeout_phase'] == phase
    assert record['request_bytes'] == 2
    assert len(calls) == 1


def test_legacy_provider_errors_keep_original_record_shape():
    from app.nmm.calls import error_record

    assert error_record(NmmProviderError('network', '安全错误')) == {
        'code': 'network',
        'message': '安全错误',
        'retryable': False,
    }


def test_base_transport_timeout_stays_unspecified_and_cancellation_propagates():
    async def exercise():
        async def timeout(request):
            raise httpx.TimeoutException('private-secret', request=request)

        vision, _ = build_nmm_providers(settings(), transport=httpx.MockTransport(timeout))
        with pytest.raises(NmmProviderError) as caught:
            await vision.request('/test', 1)
        assert caught.value.timeout_phase == 'transport'
        assert caught.value.request_bytes is None
        assert '阶段未知' in str(caught.value)

        async def canceled(request):
            raise asyncio.CancelledError()

        vision, _ = build_nmm_providers(settings(), transport=httpx.MockTransport(canceled))
        with pytest.raises(asyncio.CancelledError):
            await vision.request('/test', 1)

    asyncio.run(exercise())


@pytest.mark.parametrize(
    'values',
    [
        {'timeout_phase': 'private-secret'},
        {'request_bytes': -1},
        {'request_bytes': True},
        {'request_bytes': 'private-secret'},
    ],
)
def test_diagnostic_fields_reject_uncontrolled_values(values):
    with pytest.raises(ValueError):
        NmmProviderError('timeout', '安全错误', **values)


def test_workflow_error_summary_keeps_safe_timeout_diagnostics(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from uuid import uuid4
    from app.nmm import workflow_service

    error = NmmProviderError(
        'timeout',
        'NMM 视觉请求超时：上传请求',
        retryable=True,
        outcome_unknown=True,
        timeout_phase='write',
        request_bytes=123,
    )

    async def invoke(*args, **kwargs):
        raise error

    @asynccontextmanager
    async def open_store():
        yield None

    project = SimpleNamespace(
        active_job_id=7,
        revision=1,
        workflow_thread_id='thread',
        status='generating',
        selected_candidate_id=None,
        meta={},
    )
    session = MagicMock()
    session.__enter__.return_value = session
    session.get.return_value = project
    session.scalar.return_value = project
    monkeypatch.setattr(workflow_service, 'Session', lambda *args: session)
    monkeypatch.setattr(
        workflow_service, 'build_workflow', lambda *args: SimpleNamespace(ainvoke=invoke)
    )
    service = object.__new__(workflow_service.NmmWorkflowService)
    service.engine = object()
    service.checkpoint_store = SimpleNamespace(open=open_store)
    service._terminal = MagicMock()
    result = asyncio.run(service._execute(uuid4(), 1, 'thread', {}, SimpleNamespace(id=7)))
    assert result['status'] == 'failed'
    assert project.meta['error_info']['timeout_phase'] == 'write'
    assert project.meta['error_info']['request_bytes'] == 123


def test_immediate_builtin_timeout_does_not_claim_overall_deadline():
    calls = []

    async def handler(request):
        calls.append(request)
        raise TimeoutError('private-secret')

    vision, _ = build_nmm_providers(settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(NmmProviderError) as caught:
        asyncio.run(vision.request('/test', 1, content=b'{}'))
    assert caught.value.timeout_phase == 'transport'
    assert '阶段未知' in str(caught.value)
    assert caught.value.outcome_unknown and len(calls) == 1


@pytest.mark.parametrize('capability,write_seconds', [('vision', 120), ('image', 30)])
def test_capability_upload_budget_preserves_other_timeout_limits(capability, write_seconds):
    captured = []

    def handler(request):
        captured.append(request.extensions['timeout'])
        return httpx.Response(200, json={})

    vision, image = build_nmm_providers(settings(), transport=httpx.MockTransport(handler))
    provider = vision if capability == 'vision' else image
    assert provider.timeout_seconds == 240
    asyncio.run(provider.request('/test', 1, content=b'{}'))
    assert captured == [{'connect': 15, 'write': write_seconds, 'read': 240, 'pool': 15}]


@pytest.mark.parametrize('size', [(16, 16), (1536, 1024), (1024, 1536)])
def test_wrong_returned_canvas_is_rejected_without_resizing_or_retry(size):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=image_response(raw=make_png(size=size)))

    with pytest.raises(NmmProviderError) as caught:
        run_image(handler)
    assert caught.value.code == 'invalid_output'
    assert caught.value.usage.output_images == 0
    assert len(calls) == 1


@pytest.mark.parametrize(
    'size,canvas,content_box',
    [
        ((1536, 768), (1536, 1024), (0, 128, 1536, 896)),
        ((768, 1536), (1024, 1536), (128, 0, 896, 1536)),
        ((1024, 1024), (1024, 1024), (0, 0, 1024, 1024)),
        ((16, 10), (1536, 1024), (760, 507, 776, 517)),
        ((1, 1), (1024, 1024), (511, 511, 512, 512)),
    ],
)
def test_identity_canvas_roundtrip_retains_original_coordinates(size, canvas, content_box):
    from email import policy
    from email.parser import BytesParser
    from io import BytesIO
    import numpy as np
    from PIL import Image
    from app.nmm.providers import ImageInput

    source = Image.new('RGB', size, (30, 40, 50))
    source.paste((210, 110, 10), (0, 0, max(1, size[0] // 3), size[1]))
    output = BytesIO()
    source.save(output, 'PNG')
    original = ImageInput(output.getvalue(), 'image/png')
    mask = make_inputs(size)['mask']

    def handler(request):
        message = BytesParser(policy=policy.default).parsebytes(
            ('Content-Type: ' + request.headers['content-type'] + '\r\n\r\n').encode()
            + request.read()
        )
        values = {
            part.get_param('name', header='content-disposition'): part.get_payload(decode=True)
            for part in message.iter_parts()
        }
        assert values['size'].decode() == f'{canvas[0]}x{canvas[1]}'
        with (
            Image.open(BytesIO(values['image[]'])) as sent,
            Image.open(BytesIO(values['mask'])) as alpha,
        ):
            assert sent.size == alpha.size == canvas
            assert alpha.getchannel('A').point(lambda value: 255 - value).getbbox() == content_box
            np.testing.assert_array_equal(np.asarray(sent.crop(content_box)), np.asarray(source))
        # Identity service response uses the actual input canvas, not the original size.
        return httpx.Response(200, json=image_response(raw=values['image[]']))

    _, provider = build_nmm_providers(settings(), transport=httpx.MockTransport(handler))
    result = asyncio.run(provider.edit(original, mask, [], prompt('generate'), 1))
    with Image.open(BytesIO(result.value[0])) as restored:
        assert restored.size == size
        np.testing.assert_array_equal(np.asarray(restored.convert('RGB')), np.asarray(source))


def test_preprocessing_protocol_change_invalidates_old_configuration_quote():
    from app.nmm.provider_config import configuration_id

    _, image = build_nmm_providers(settings())
    legacy_config = {
        'provider': 'openai',
        'model': settings()['IMAGE_MODEL'],
        'base_url': settings()['IMAGE_BASE_URL'],
        'quality': 'medium',
        'output_format': 'png',
        'background': 'opaque',
        'max_references': 3,
    }
    assert image.configuration_id != configuration_id(legacy_config, settings()['IMAGE_API_KEY'])


def test_transparent_output_canvas_is_rejected_before_inverse_mapping():
    from io import BytesIO
    from PIL import Image

    stream = BytesIO()
    Image.new('RGBA', (1024, 1024), (10, 20, 30, 100)).save(stream, 'PNG')
    with pytest.raises(NmmProviderError) as caught:
        run_image(lambda request: httpx.Response(200, json=image_response(raw=stream.getvalue())))
    assert caught.value.code == 'invalid_output'
