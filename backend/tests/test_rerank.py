"""Cross-encoder rerank: provider contract, threshold semantics and safe fallback."""

import asyncio
import json

import httpx
import pytest

from app.errors import ProviderError
from app.rerank import DashScopeReranker
from app.retrieval_types import RetrievalResult
from app.services.retrieval import RetrievalService, apply_threshold

ENDPOINT = 'https://dashscope.example.com/api/v1/services/rerank/text-rerank/text-rerank'
SETTINGS = {'RERANK_BASE_URL': ENDPOINT, 'RERANK_MODEL': 'qwen3-rerank', 'RERANK_API_KEY': 'k'}


def provider(handler):
    return DashScopeReranker(SETTINGS, transport=httpx.MockTransport(handler))


def reply(results, status=200):
    return lambda request: httpx.Response(status, json={'output': {'results': results}})


def test_request_shape_and_scores_returned_in_input_order():
    seen = {}

    def handler(request):
        seen['body'] = json.loads(request.content)
        seen['auth'] = request.headers['authorization']
        return httpx.Response(
            200,
            json={
                'output': {
                    'results': [
                        {'index': 1, 'relevance_score': 0.9},
                        {'index': 0, 'relevance_score': 0.2},
                    ]
                }
            },
        )

    scores = asyncio.run(provider(handler).rerank('问题', ['甲', '乙']))
    assert scores == [0.2, 0.9]
    assert seen['body'] == {
        'model': 'qwen3-rerank',
        'input': {'query': '问题', 'documents': ['甲', '乙']},
        'parameters': {'return_documents': False},
    }
    assert seen['auth'] == 'Bearer k'


@pytest.mark.parametrize(
    'handler',
    [
        reply([], status=500),
        reply([{'index': 5, 'relevance_score': 0.1}]),
        reply([{'index': 0, 'relevance_score': 0.1}]),
        reply([{'index': 0, 'relevance_score': True}, {'index': 1, 'relevance_score': 0.1}]),
        lambda request: httpx.Response(200, text='not json'),
    ],
)
def test_malformed_or_failed_responses_raise_safe_errors(handler):
    with pytest.raises(ProviderError) as error:
        asyncio.run(provider(handler).rerank('q', ['a', 'b']))
    assert 'not json' not in str(error.value)


def test_unconfigured_reranker_never_calls_out():
    assert not DashScopeReranker({}).configured
    assert not DashScopeReranker({**SETTINGS, 'RERANK_API_KEY': ''}).configured
    with pytest.raises(ProviderError):
        asyncio.run(DashScopeReranker({}).rerank('q', ['a']))


class FakeReranker:
    model = 'fake-rerank'
    configured = True

    def __init__(self, scores=None, error=None, delay=0):
        self.scores, self.error, self.delay, self.calls = scores, error, delay, []

    async def rerank(self, query, documents):
        self.calls.append((query, documents))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.scores


def hit(key, *reasons):
    return {
        'key': key,
        'type': 'technique',
        'id': int(key.split(':')[1]),
        'title': key,
        'excerpt': '摘录',
        'source_ids': [],
        'sources': [],
        'rank': 0,
        'score': 0,
        'reasons': list(reasons),
    }


def fused(*hits):
    return RetrievalResult(list(hits), 'hybrid', 'hybrid', 'v', {'latency_ms': 1})


def service(reranker, **kwargs):
    return RetrievalService(None, lambda: None, reranker_getter=lambda: reranker, **kwargs)


def test_rerank_reorders_and_drops_candidates_below_threshold():
    reranker = FakeReranker([0.1, 0.8, 0.5])
    result = asyncio.run(
        service(reranker, rerank_min_score=0.3).rerank(
            '问题', fused(hit('technique:1'), hit('technique:2'), hit('technique:3')), 5
        )
    )
    assert [h['key'] for h in result.hits] == ['technique:2', 'technique:3']
    assert [h['rank'] for h in result.hits] == [1, 2]
    assert result.effective_strategy == 'rerank'
    assert result.diagnostics['rerank_dropped'] == 1
    assert reranker.calls[0][1][0] == 'technique:1\n摘录'


def test_all_candidates_below_threshold_means_no_evidence():
    result = asyncio.run(
        service(FakeReranker([0.01, 0.02]), rerank_min_score=0.3).rerank(
            '税率', fused(hit('technique:1'), hit('technique:2')), 5
        )
    )
    assert result.hits == []
    assert result.effective_strategy == 'rerank'


def test_exact_identifier_hit_is_kept_and_first_even_with_low_score():
    candidates = fused(hit('technique:1'), hit('technique:2', '编号规范化精确匹配（规则提升）'))
    result = asyncio.run(
        service(FakeReranker([0.9, 0.05]), rerank_min_score=0.3).rerank('AK11190', candidates, 5)
    )
    assert [h['key'] for h in result.hits] == ['technique:2', 'technique:1']


@pytest.mark.parametrize(
    'reranker,reason',
    [
        (None, 'unconfigured'),
        (FakeReranker(error=ProviderError('x')), 'provider_error'),
        (FakeReranker([0.9], delay=1), 'timeout'),
    ],
)
def test_rerank_failure_keeps_fused_order_unfiltered(reranker, reason):
    candidates = fused(hit('technique:1'), hit('technique:2'))
    result = asyncio.run(
        service(reranker, rerank_min_score=0.99, timeout=0.05).rerank('q', candidates, 1)
    )
    assert [h['key'] for h in result.hits] == ['technique:1']
    assert result.effective_strategy == 'hybrid'
    assert result.requested_strategy == 'rerank'
    assert result.diagnostics['rerank_fallback_reason'] == reason


def test_empty_recall_is_a_rerank_answer_without_a_call():
    reranker = FakeReranker([])
    result = asyncio.run(service(reranker).rerank('q', fused(), 5))
    assert result.hits == [] and result.effective_strategy == 'rerank'
    assert reranker.calls == []


def test_threshold_helper_matches_live_filter():
    scored = [
        {**hit('technique:1'), 'score': 0.9},
        {**hit('technique:2', '别名精确匹配'), 'score': 0.0},
        {**hit('technique:3'), 'score': 0.2},
    ]
    assert [h['key'] for h in apply_threshold(scored, 0.5)] == ['technique:1', 'technique:2']


def test_invalid_configuration_rejected():
    with pytest.raises(ValueError):
        service(None, rerank_min_score=1.5)
    with pytest.raises(ValueError):
        service(None, rerank_candidates=0)
    with pytest.raises(ValueError):
        RetrievalService(None, lambda: None, mode='semantic')
