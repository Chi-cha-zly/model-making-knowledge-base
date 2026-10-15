"""Cross-encoder reranking over the DashScope native text-rerank endpoint.

Scores are relative to one request (0–1, per the provider docs); a fixed cut-off
is an empirical calibration on our own labelled set, not a confidence level.
"""

import math

import httpx

from .errors import ProviderError
from .runtime_config import loopback_endpoint, valid_endpoint

MAX_DOCUMENTS = 500
# qwen3-rerank accepts 4000 tokens per item; 3000 CJK characters stays below it.
MAX_DOCUMENT_CHARS = 3000
MAX_QUERY_CHARS = 2000


class DashScopeReranker:
    def __init__(self, settings, transport=None, timeout=15):
        self.model = settings.get('RERANK_MODEL', '').strip() or None
        self.endpoint = settings.get('RERANK_BASE_URL', '').strip()
        self.api_key = settings.get('RERANK_API_KEY', '').strip()
        self.transport = transport
        self.timeout = timeout
        self.configured = bool(
            self.model
            and valid_endpoint(self.endpoint)
            and (self.api_key or loopback_endpoint(self.endpoint))
        )

    async def rerank(self, query, documents):
        """Return one score per input document, in input order."""
        if not self.configured:
            raise ProviderError(
                '尚未配置重排模型；请设置 RERANK_BASE_URL、RERANK_MODEL 和 RERANK_API_KEY。'
            )
        if not documents:
            return []
        if len(documents) > MAX_DOCUMENTS:
            raise ProviderError('重排候选过多。')
        body = {
            'model': self.model,
            'input': {
                'query': query[:MAX_QUERY_CHARS],
                'documents': [text[:MAX_DOCUMENT_CHARS] or ' ' for text in documents],
            },
            'parameters': {'return_documents': False},
        }
        headers = {'Authorization': f'Bearer {self.api_key}'} if self.api_key else {}
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                response = await client.post(self.endpoint, json=body, headers=headers)
        except httpx.TimeoutException:
            raise ProviderError('重排服务超时。') from None
        except httpx.HTTPError:
            raise ProviderError('无法连接重排服务。') from None
        if response.status_code >= 400:
            raise ProviderError(f'重排服务请求失败（HTTP {response.status_code}）。')
        try:
            results = response.json()['output']['results']
            scores = [None] * len(documents)
            for item in results:
                index, score = item['index'], item['relevance_score']
                if (
                    type(index) is not int
                    or not 0 <= index < len(documents)
                    or scores[index] is not None
                    or isinstance(score, bool)
                    or not isinstance(score, (int, float))
                    or not math.isfinite(score)
                ):
                    raise ValueError
                scores[index] = float(score)
        except (ValueError, KeyError, TypeError):
            raise ProviderError('重排服务返回了无法解析的结果。') from None
        if any(score is None for score in scores):
            raise ProviderError('重排服务未返回全部候选的分数。')
        return scores
