"""LangChain adapter with bounded calls and sanitized, explicit LangSmith traces."""
import asyncio
import math
import json
from contextlib import aclosing, asynccontextmanager
from time import perf_counter

import httpx
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langsmith import trace as smith_trace, tracing_context

from .errors import ProviderError
from .runtime_config import (read_settings, normalize_model, output_limit, valid_endpoint, loopback_endpoint,
                             langsmith_config, langsmith_client)


def safe_error(exc):
    status = getattr(exc, 'status_code', None)
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)) or type(exc).__name__ == 'APITimeoutError':
        return ProviderError('模型服务超时，已保留部分文本；请重试。')
    if isinstance(status, int):
        return ProviderError(f'模型服务请求失败（HTTP {status}）；请检查配置或稍后重试。')
    return ProviderError('模型服务调用失败；请检查地址、网络和服务状态。')


def text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ''.join(block.get('text', '') for block in content
                       if isinstance(block, dict) and block.get('type') == 'text' and isinstance(block.get('text'), str))
    return ''


class _CompleteSSEStream(httpx.AsyncByteStream):
    """Require a complete SSE terminator while leaving model parsing to the SDK."""

    def __init__(self, response):
        self.response = response

    async def __aiter__(self):
        # HTTPX handles decompression, UTF-8 boundaries and CR/LF line endings.
        # Keep only a small terminator candidate, never a copy of the full answer.
        candidate = ''
        async for line in self.response.aiter_lines():
            if line.startswith('data:'):
                value = line[5:].removeprefix(' ')
                candidate = value if not candidate and value == '[DONE]' else 'other'
            elif not line:
                done = candidate == '[DONE]'
                candidate = ''
                yield b'\n'
                if done:
                    return
                continue
            yield (line + '\n').encode('utf-8')
        raise ProviderError('模型连接中断，未收到完整结束标记；已保留部分文本。')

    async def aclose(self):
        await self.response.aclose()


class _StreamingClient(httpx.AsyncClient):
    async def send(self, request, **kwargs):
        response = await super().send(request, **kwargs)
        if response.is_error:
            return response
        headers = response.headers.copy()
        # The wrapped stream is already decoded by HTTPX and normalized to UTF-8.
        for name in ('content-encoding', 'content-length'):
            headers.pop(name, None)
        return httpx.Response(response.status_code, headers=headers, request=response.request,
                              extensions=response.extensions, stream=_CompleteSSEStream(response))


def validate_vectors(vectors, count):
    """Validate real numeric results without normalizing or inventing embeddings."""
    if not isinstance(vectors, list) or len(vectors) != count or not vectors:
        raise ProviderError('向量服务返回数量不匹配。')
    dimensions = len(vectors[0]) if isinstance(vectors[0], list) else 0
    if not 1 <= dimensions <= 65536 or any(
        not isinstance(vector, list) or len(vector) != dimensions or any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            for value in vector) for vector in vectors):
        raise ProviderError('向量服务返回无效数值或不一致的维度。')
    return dimensions


async def _check_embedding_response(response):
    if response.is_error:
        return
    await response.aread()
    data = response.json().get('data')
    count = len(json.loads(response.request.content)['input'])
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise ProviderError('向量服务返回无效数据。')
    indices = [item.get('index') for item in data]
    if any(type(index) is not int for index in indices) or sorted(indices) != list(range(count)):
        raise ProviderError('向量服务返回了不完整或重复的输入索引。')
    # Validate before SDK coercion can turn booleans or strings into floats.
    validate_vectors([item.get('embedding') for item in data], count)


class _OrderedEmbeddingClient:
    """LangChain uses response list order; enforce the API's explicit input index."""
    def __init__(self, client):
        self.client = client

    async def create(self, *, input, **kwargs):
        response = await self.client.create(input=input, **kwargs)
        data = response.data
        indices = [item.index for item in data]
        if (any(type(index) is not int for index in indices)
                or sorted(indices) != list(range(len(input)))):
            raise ProviderError('向量服务返回了不完整或重复的输入索引。')
        ordered = sorted(data, key=lambda item: item.index)
        validate_vectors([item.embedding for item in ordered], len(input))
        return {'data': [{'embedding': item.embedding} for item in ordered]}


class LangChainProvider:
    def __init__(self, settings=None, transport=None):
        self.settings = dict(read_settings() if settings is None else settings)
        self.model = normalize_model(self.settings.get('AI_MODEL', '')) or None
        self.base_url = self.settings.get('AI_BASE_URL', '').rstrip('/')
        self.api_key = self.settings.get('AI_API_KEY', '')
        self.reasoning_effort = self.settings.get('AI_REASONING_EFFORT') or 'low'
        if self.reasoning_effort == 'auto': self.reasoning_effort = None
        self.max_output_tokens = output_limit(self.settings)
        self.transport = transport
        self.embedding_model = self.settings.get('EMBEDDING_MODEL', '').strip() or None
        self.embedding_base_url = self.settings.get('EMBEDDING_BASE_URL', '').strip().rstrip('/')
        self.embedding_api_key = self.settings.get('EMBEDDING_API_KEY', '').strip()
        self.embedding_configured = bool(self.embedding_model and valid_endpoint(self.embedding_base_url)
                                         and (self.embedding_api_key or loopback_endpoint(self.embedding_base_url)))
        self.configured = bool(self.model and valid_endpoint(self.base_url) and
                               (self.api_key or loopback_endpoint(self.base_url)))

    async def embed(self, texts):
        """Explicit embedding call; never falls back to chat/OpenAI ambient settings."""
        if not self.embedding_configured:
            raise ProviderError('尚未配置向量模型；请设置 EMBEDDING_BASE_URL、EMBEDDING_MODEL 和 EMBEDDING_API_KEY。')
        if (not isinstance(texts, list) or not 1 <= len(texts) <= 64
                or any(not isinstance(text, str) or not text.strip() or len(text) > 12000 for text in texts)
                or sum(map(len, texts)) > 128000):
            raise ProviderError('向量批次须为 1–64 个非空文本，每项至多 12000 字符，总计至多 128000 字符。')
        failure = None
        try:
            with tracing_context(enabled=False):
                async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=15), transport=self.transport,
                                             event_hooks={'response': [_check_embedding_response]}) as client:
                    encoder = OpenAIEmbeddings(model=self.embedding_model,
                        base_url=self.embedding_base_url, api_key=self.embedding_api_key or 'local-no-key',
                        openai_api_type='', openai_proxy='', openai_organization='',
                        check_embedding_ctx_length=False, chunk_size=64, max_retries=0,
                        request_timeout=90, http_async_client=client,
                        model_kwargs={'encoding_format': 'float'})
                    encoder.async_client = _OrderedEmbeddingClient(encoder.async_client)
                    vectors = await encoder.aembed_documents(texts)
                    validate_vectors(vectors, len(texts))
                    return vectors
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = ProviderError(str(exc)) if isinstance(exc, ProviderError) else safe_error(exc)
        raise failure from None

    @classmethod
    def from_env(cls):
        return cls()

    @asynccontextmanager
    async def _model(self, model, *, streaming=False):
        if not self.configured:
            raise ProviderError('尚未配置模型；请设置 AI_BASE_URL、AI_MODEL 和 AI_API_KEY。')
        responses = []
        async def remember(response):
            responses.append(response)
        client_type = _StreamingClient if streaming else httpx.AsyncClient
        async with client_type(timeout=httpx.Timeout(90, connect=15), transport=self.transport,
                                     event_hooks={'response': [remember]}) as client:
            try:
                yield ChatOpenAI(model=model, api_key=self.api_key or 'local-no-key', base_url=self.base_url,
                             **({'reasoning_effort': self.reasoning_effort} if self.reasoning_effort is not None else {}), max_tokens=self.max_output_tokens,
                             temperature=None, max_retries=0, timeout=90, stream_usage=True,
                             use_responses_api=False, http_async_client=client)
            finally:
                for response in responses:
                    await response.aclose()

    @asynccontextmanager
    async def _trace(self, messages, model, enabled=None, metadata=None):
        config = langsmith_config(self.settings)
        active = config['configured'] and (config['tracing_enabled'] if enabled is None else enabled)
        # The raw SDK call never has tracing callbacks. Only sanitized results cross this boundary.
        if not active:
            with tracing_context(enabled=False):
                yield None
            return
        client = langsmith_client(self.settings)
        safe_metadata = {key: value for key, value in (metadata or {}).items()
                         if key in ('experiment_id', 'case_id', 'dataset_id', 'variant', 'run_id', 'snapshot_hash')
                         and isinstance(value, (str, int, float, bool))}
        try:
            with tracing_context(enabled=True, client=client, project_name=config['project'], replicas=[]):
                with smith_trace('model-answer', run_type='llm', inputs={'messages': messages},
                                 metadata={**safe_metadata, 'model': model}, client=client,
                                 project_name=config['project']) as run:
                    with tracing_context(enabled=False):
                        yield run
        finally:
            await asyncio.to_thread(client.close, timeout=5)

    async def complete(self, messages, *, model=None, trace=None, metadata=None):
        selected = normalize_model(model) if model else self.model
        started = perf_counter()
        async with self._trace(messages, selected, trace, metadata) as run:
            failure = None
            result = None
            try:
                async with self._model(selected) as llm:
                    result = await llm.ainvoke(messages, config={'callbacks': []})
            except asyncio.CancelledError:
                if run:
                    run.end(error='模型调用已停止。')
                raise
            except Exception as exc:
                failure = ProviderError(str(exc)) if isinstance(exc, ProviderError) else safe_error(exc)
            # Raise outside except to avoid raw exception context entering LangSmith traceback.
            if failure:
                raise failure from None
            content = text_content(result.content)
            if result.response_metadata.get('finish_reason') in ('length', 'content_filter'):
                raise ProviderError('模型输出被截断或过滤；请调整要求后重试。')
            if not content.strip():
                raise ProviderError('模型服务返回空回答；请重试。')
            raw = result.usage_metadata
            usage = {key: raw[key] for key in ('input_tokens', 'output_tokens', 'total_tokens')} if raw else None
            output = {'content': content, 'usage': usage, 'latency_ms': (perf_counter()-started)*1000, 'model': selected}
            if run:
                run.end(outputs=output)
            return output

    async def stream(self, messages):
        async with self._trace(messages, self.model) as run:
            failure = None
            parts, finish = [], None
            try:
                async with self._model(self.model, streaming=True) as llm:
                    async with aclosing(llm.astream(messages, config={'callbacks': []})) as chunks:
                        async for chunk in chunks:
                            finish = chunk.response_metadata.get('finish_reason') or finish
                            text = text_content(chunk.content)
                            if text:
                                parts.append(text)
                                yield text
                            if finish in ('length', 'content_filter'):
                                raise ProviderError('模型输出被截断或过滤；已保留部分文本，请调整要求后重试。')
            except (asyncio.CancelledError, GeneratorExit):
                if run:
                    run.end(error='模型调用已停止。')
                raise
            except Exception as exc:
                failure = ProviderError(str(exc)) if isinstance(exc, ProviderError) else safe_error(exc)
            if failure:
                raise failure from None
            # Some compatible local services emit only [DONE], without finish_reason.
            if finish not in (None, 'stop') or not ''.join(parts).strip():
                raise ProviderError('模型连接中断或返回空回答，未收到完整结束标记；已保留部分文本。')
            if run:
                run.end(outputs={'content': ''.join(parts), 'model': self.model})
