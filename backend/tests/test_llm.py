import asyncio
from contextlib import nullcontext
import json

import httpx
import pytest


def settings(**extra):
    return {
        'AI_BASE_URL': 'https://model.invalid/v1',
        'AI_API_KEY': 'fake-only',
        'AI_MODEL': 'test-model',
        'LANGSMITH_TRACING': 'false',
        **extra,
    }


def completion(usage=True):
    result = {
        'id': 'chatcmpl-test',
        'object': 'chat.completion',
        'created': 1,
        'model': 'test-model',
        'choices': [
            {
                'index': 0,
                'message': {'role': 'assistant', 'content': '测试回答'},
                'finish_reason': 'stop',
            }
        ],
    }
    if usage:
        result['usage'] = {'prompt_tokens': 12, 'completion_tokens': 4, 'total_tokens': 16}
    return result


def test_config_whitespace_and_explicit_disabled(tmp_path, monkeypatch):
    from app.runtime_config import read_settings, integration_status, langsmith_client

    path = tmp_path / 'fake.env'
    path.write_text('AI_MODEL=" old "\nLANGSMITH_TRACING=true\nLANGSMITH_API_KEY=fake-only\n')
    values = read_settings(path, {'AI_MODEL': ' "new" ', 'LANGSMITH_TRACING': 'false'})
    assert values['AI_MODEL'] == 'new'
    assert integration_status(settings=values)['langsmith']['tracing_enabled'] is False
    monkeypatch.setenv('LANGSMITH_API_KEY', 'ambient-secret')
    assert langsmith_client({}) is None
    assert integration_status(settings={})['model_configured'] is False


def test_complete_real_sdk_bounded_request_and_fresh_usage(monkeypatch):
    from app.llm import LangChainProvider

    monkeypatch.setenv('LANGSMITH_TRACING', 'true')
    monkeypatch.setenv('LANGSMITH_API_KEY', 'ambient-secret')
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=completion(usage=len(requests) == 1))

    async def go():
        provider = LangChainProvider(
            settings=settings(AI_MAX_OUTPUT_TOKENS='999999'), transport=httpx.MockTransport(handler)
        )
        a = await provider.complete([{'role': 'user', 'content': 'hello'}])
        b = await provider.complete([{'role': 'user', 'content': 'hello'}], model='alternate')
        assert a['content'] == '测试回答' and a['usage'] == {
            'input_tokens': 12,
            'output_tokens': 4,
            'total_tokens': 16,
        }
        assert b['usage'] is None and b['model'] == 'alternate'
        assert a['latency_ms'] >= 0

    asyncio.run(go())
    assert requests[0]['max_completion_tokens'] == 2048
    assert 'temperature' not in requests[0]
    assert requests[1]['model'] == 'alternate'


@pytest.mark.parametrize('status', [401, 429, 500])
def test_upstream_errors_are_safe_and_not_retried(status):
    from app.llm import LangChainProvider
    from app.errors import ProviderError

    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(status, json={'error': {'message': 'credential-secret-body'}})

    async def go():
        with pytest.raises(ProviderError) as err:
            await LangChainProvider(
                settings=settings(), transport=httpx.MockTransport(handler)
            ).complete([{'role': 'user', 'content': 'hi'}])
        assert 'credential-secret' not in str(err.value)

    asyncio.run(go())
    assert len(calls) == 1


class SSE(httpx.AsyncByteStream):
    def __init__(self, finish='stop', block=False):
        self.finish, self.block, self.closed = finish, block, False

    async def __aiter__(self):
        event = {
            'id': 'a',
            'object': 'chat.completion.chunk',
            'created': 1,
            'model': 'test-model',
            'choices': [
                {
                    'index': 0,
                    'delta': {'role': 'assistant', 'content': 'hello'},
                    'finish_reason': None,
                }
            ],
        }
        yield ('data: ' + json.dumps(event) + '\n\n').encode()
        if self.block:
            await asyncio.sleep(3600)
        if self.finish:
            event['choices'][0].update(delta={}, finish_reason=self.finish)
            yield ('data: ' + json.dumps(event) + '\n\ndata: [DONE]\n\n').encode()

    async def aclose(self):
        self.closed = True


def test_stream_cancel_closes_upstream():
    from app.llm import LangChainProvider

    upstream = SSE(block=True)

    async def go():
        provider = LangChainProvider(
            settings=settings(),
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=upstream)),
        )
        received = asyncio.Event()

        async def consume():
            async for text in provider.stream([{'role': 'user', 'content': 'hi'}]):
                assert text == 'hello'
                received.set()

        task = asyncio.create_task(consume())
        await asyncio.wait_for(received.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert upstream.closed

    asyncio.run(go())


@pytest.mark.parametrize('finish', [None, 'length', 'content_filter'])
def test_stream_incomplete_fails(finish):
    from app.llm import LangChainProvider
    from app.errors import ProviderError

    upstream = SSE(finish=finish)

    async def go():
        p = LangChainProvider(
            settings=settings(),
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=upstream)),
        )
        with pytest.raises(ProviderError):
            async for _ in p.stream([{'role': 'user', 'content': 'hi'}]):
                pass
        assert upstream.closed

    asyncio.run(go())


def test_disabled_tracing_never_constructs_client(monkeypatch):
    import app.llm as module

    monkeypatch.setenv('LANGSMITH_TRACING', 'true')
    monkeypatch.setenv('LANGCHAIN_TRACING_V2', 'true')
    monkeypatch.setenv('LANGSMITH_API_KEY', 'ambient-secret')

    def forbidden(*args, **kwargs):
        raise AssertionError('unexpected tracing client')

    monkeypatch.setattr(module, 'langsmith_client', forbidden)
    import langsmith

    monkeypatch.setattr(langsmith, 'Client', forbidden)

    async def go():
        p = module.LangChainProvider(
            settings=settings(),
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=completion())),
        )
        assert (await p.complete([{'role': 'user', 'content': 'hi'}]))['content'] == '测试回答'

    asyncio.run(go())


def test_enabled_trace_sees_only_sanitized_exception(monkeypatch):
    from contextlib import contextmanager
    import app.llm as module
    from langsmith import get_tracing_context
    from app.errors import ProviderError

    seen = []

    @contextmanager
    def capture(*args, **kwargs):
        try:
            yield None
        except Exception as exc:
            import traceback

            seen.append(''.join(traceback.format_exception(exc)))
            raise

    monkeypatch.setattr(module, 'smith_trace', capture)
    from types import SimpleNamespace

    monkeypatch.setattr(
        module, 'langsmith_client', lambda _: SimpleNamespace(close=lambda **_: None)
    )

    def handler(_):
        assert get_tracing_context()['enabled'] is False
        return httpx.Response(401, json={'error': {'message': 'super-secret-error-body'}})

    async def go():
        p = module.LangChainProvider(
            settings=settings(LANGSMITH_API_KEY='fake-only'), transport=httpx.MockTransport(handler)
        )
        with pytest.raises(ProviderError):
            await p.complete([{'role': 'user', 'content': 'hi'}], trace=True)

    asyncio.run(go())
    assert seen and 'super-secret-error-body' not in seen[0]


def test_missing_configuration_and_empty_answer():
    from app.llm import LangChainProvider
    from app.errors import ProviderError

    async def go():
        with pytest.raises(ProviderError):
            await LangChainProvider(settings={}).complete([])
        response = completion()
        response['choices'][0]['message']['content'] = ''
        with pytest.raises(ProviderError):
            await LangChainProvider(
                settings=settings(),
                transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response)),
            ).complete([])

    asyncio.run(go())


def test_trace_lifecycle_explicit_replica_isolation(monkeypatch):
    from contextlib import contextmanager
    import app.llm as module
    from langsmith import get_tracing_context

    observed = []

    class Client:
        def close(self, timeout=None):
            observed.append(('closed', timeout))

    @contextmanager
    def capture(*args, **kwargs):
        assert get_tracing_context()['replicas'] == []
        yield None

    monkeypatch.setattr(module, 'smith_trace', capture)
    monkeypatch.setattr(module, 'langsmith_client', lambda _: Client())

    async def go():
        p = module.LangChainProvider(
            settings=settings(LANGSMITH_API_KEY='fake-only'),
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=completion())),
        )
        await p.complete([{'role': 'user', 'content': 'hi'}], trace=True)

    asyncio.run(go())
    assert observed == [('closed', 5)]


def test_invalid_workspace_is_unconfigured():
    from app.runtime_config import langsmith_config, langsmith_client

    values = settings(LANGSMITH_API_KEY='fake', LANGSMITH_WORKSPACE_ID='not-a-uuid')
    assert langsmith_config(values)['configured'] is False
    assert 'not-a-uuid' not in langsmith_config(values)['message']
    assert langsmith_client(values) is None


@pytest.mark.parametrize('ending', ['\n\n', '\r\n\r\n'])
def test_finish_reason_without_done_is_incomplete(ending):
    from app.llm import LangChainProvider
    from app.errors import ProviderError

    body = (
        'data: '
        + json.dumps(
            {'choices': [{'index': 0, 'delta': {'content': 'partial'}, 'finish_reason': 'stop'}]}
        )
        + ending
    )

    async def go():
        provider = LangChainProvider(
            settings=settings(),
            transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body)),
        )
        parts = []
        with pytest.raises(ProviderError, match='中断|结束'):
            async for part in provider.stream([{'role': 'user', 'content': 'hi'}]):
                parts.append(part)
        assert parts == ['partial']

    asyncio.run(go())


@pytest.mark.parametrize('finish', ['length', 'content_filter'])
def test_truncated_final_chunk_preserves_last_text(finish):
    from app.llm import LangChainProvider
    from app.errors import ProviderError

    body = (
        'data: '
        + json.dumps(
            {'choices': [{'index': 0, 'delta': {'content': 'partial'}, 'finish_reason': finish}]}
        )
        + '\n\ndata: [DONE]\n\n'
    )

    async def go():
        provider = LangChainProvider(
            settings=settings(),
            transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body)),
        )
        parts = []
        with pytest.raises(ProviderError, match='截断|过滤'):
            async for part in provider.stream([{'role': 'user', 'content': 'hi'}]):
                parts.append(part)
        assert parts == ['partial']

    asyncio.run(go())


@pytest.mark.parametrize('compressed', [False, True])
@pytest.mark.parametrize('newline', ['\n', '\r\n', '\r'])
def test_fragmented_sse_done_and_unicode_are_preserved(compressed, newline):
    import gzip
    from app.llm import LangChainProvider

    body = (
        ': keepalive'
        + newline * 2
        + 'data: '
        + json.dumps(
            {'choices': [{'index': 0, 'delta': {'content': '你好'}, 'finish_reason': 'stop'}]},
            ensure_ascii=False,
        )
        + newline * 2
        + 'data: [DONE]'
        + newline * 2
    ).encode()
    if compressed:
        body = gzip.compress(body)

    class Bytes(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            for byte in body:
                yield bytes([byte])

        async def aclose(self):
            self.closed = True

    upstream = Bytes()

    async def go():
        provider = LangChainProvider(
            settings=settings(),
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    headers={
                        'Content-Type': 'text/event-stream',
                        **({'Content-Encoding': 'gzip'} if compressed else {}),
                    },
                    stream=upstream,
                )
            ),
        )
        assert [part async for part in provider.stream([{'role': 'user', 'content': 'hi'}])] == [
            '你好'
        ]
        assert upstream.closed

    asyncio.run(go())


@pytest.mark.parametrize(
    'payload',
    [
        'not-json-secret',
        '{"error":{"message":"upstream-secret"}}',
        '{"choices":[{"delta":{"content":42}}]}',
    ],
)
def test_stream_bad_payloads_are_sanitized(payload):
    from app.errors import ProviderError
    from app.llm import LangChainProvider

    body = (
        'data: {"choices":[{"delta":{"content":"partial"}}]}\n\ndata: '
        + payload
        + '\n\ndata: [DONE]\n\n'
    )

    async def go():
        provider = LangChainProvider(
            settings=settings(),
            transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body)),
        )
        parts = []
        with pytest.raises(ProviderError) as error:
            async for part in provider.stream([{'role': 'user', 'content': 'hi'}]):
                parts.append(part)
        assert parts == ['partial']
        assert 'secret' not in str(error.value)

    # The SDK warns while serializing this intentionally malformed delta.
    warning = (
        pytest.warns(UserWarning, match='Pydantic serializer warnings')
        if '42' in payload
        else nullcontext()
    )
    with warning:
        asyncio.run(go())
