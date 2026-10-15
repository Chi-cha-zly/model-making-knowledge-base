"""Rerank evaluation runs on the same snapshot and can be swept offline."""

import asyncio
import json

from pg_test_support import test_database_url
from test_hybrid_retrieval import QueryProvider, seed
from test_rerank import FakeReranker

from app.services.evaluation import EvaluationService
from app.services.retrieval import RetrievalService
from scripts import evaluate_retrieval


def dataset(ids):
    return {
        'id': 'file-x',
        'name': 'rerank',
        'corpus': 'live',
        'cases': [
            {
                'id': 'one',
                'query': '金属亮面',
                'category': 'semantic',
                'judgments': [{'key': f'technique:{ids[0]}', 'grade': 3}],
            },
            {'id': 'none', 'query': '税率', 'category': 'out_of_domain', 'judgments': []},
        ],
    }


def test_compare_dataset_adds_rerank_strategy_with_stored_scores():
    engine, ids = seed()
    reranker = FakeReranker([0.9, 0.1])
    retrieval = RetrievalService(
        engine,
        lambda: QueryProvider(),
        reranker_getter=lambda: reranker,
        rerank_min_score=0.5,
    )
    report = asyncio.run(
        EvaluationService(engine, retrieval).compare_dataset(dataset(ids), 5, rerank=True)
    )
    engine.dispose()
    assert report['strategies'] == ['bm25', 'hybrid', 'rerank']
    first = report['cases'][0]
    assert [h['key'] for h in first['rerank']] == [f'technique:{ids[0]}']
    assert first['metrics']['rerank']['hit_rate'] == 1
    assert {h['score'] for h in first['rerank_scored']} == {0.9, 0.1}
    assert report['rerank_min_score'] == 0.5
    assert '_pair' not in first
    rows = evaluate_retrieval.threshold_sweep(report['cases'], 5, [0.0, 0.95])
    assert rows[0]['hit_rate'] == 1 and rows[1]['hit_rate'] == 0


def test_cli_ledger_caches_calls_and_enforces_budget(tmp_path):
    engine, ids = seed()
    engine.dispose()
    data = tmp_path / 'cases.json'
    data.write_text(json.dumps({'name': 'x', 'cases': dataset(ids)['cases']}, ensure_ascii=False))
    provider, reranker = QueryProvider(), FakeReranker([0.9, 0.1])
    args = [
        '--database-url',
        test_database_url(),
        '--dataset',
        str(data),
        '--storage-dir',
        str(tmp_path / 'storage'),
        '--apply',
        '--rerank',
        '--ledger',
        str(tmp_path / 'ledger.json'),
        '--max-calls',
        '10',
        '--sweep',
        '0,0.5',
    ]
    for run in range(2):
        report = tmp_path / f'report{run}.json'
        code = evaluate_retrieval.main(
            [*args, '--report', str(report)],
            environ={},
            provider=provider,
            reranker_override=reranker,
        )
        assert code == 0
        result = json.loads(report.read_text())
        assert result['paid_calls_total'] == 4
        assert [row['threshold'] for row in result['threshold_sweep']] == [0.0, 0.5]
    assert len(provider.calls) == 2 and len(reranker.calls) == 2
    small = [*args[:-6], '--ledger', str(tmp_path / 'other.json'), '--max-calls', '1']
    assert (
        evaluate_retrieval.main(
            [*small, '--report', str(tmp_path / 'r.json')],
            environ={},
            provider=QueryProvider(),
            reranker_override=FakeReranker([0.9, 0.1]),
        )
        == 2
    )


def test_live_search_reranks_hybrid_candidates_and_can_answer_nothing():
    engine, ids = seed()
    reranker = FakeReranker([0.9, 0.1])
    retrieval = RetrievalService(
        engine,
        lambda: QueryProvider(),
        mode='rerank',
        reranker_getter=lambda: reranker,
        rerank_min_score=0.5,
    )
    result = asyncio.run(retrieval.search('金属亮面', k=5))
    assert result.effective_strategy == 'rerank'
    assert len(reranker.calls[0][1]) == 2
    assert [hit['key'] for hit in result.hits] == [f'technique:{ids[0]}']
    reranker.scores = [0.2, 0.1]
    assert asyncio.run(retrieval.search('税率', k=5)).hits == []
    assert (
        asyncio.run(retrieval.search('金属亮面', k=5, strategy='bm25')).effective_strategy == 'bm25'
    )
    engine.dispose()


def test_chat_style_search_reranks_the_current_question_not_old_turns():
    engine, ids = seed()
    reranker = FakeReranker([0.9, 0.1])
    retrieval = RetrievalService(
        engine, lambda: QueryProvider(), reranker_getter=lambda: reranker, rerank_min_score=0.5
    )
    history = '很早以前的一轮很长的讨论。' * 400 + '\n金属亮面怎么画'
    asyncio.run(retrieval.search(history, k=5, strategy='rerank', semantic_query='金属亮面怎么画'))
    assert reranker.calls[0][0] == '金属亮面怎么画'
    asyncio.run(retrieval.search(history, k=5, strategy='rerank'))
    sent = reranker.calls[1][0]
    assert len(sent) == 2000 and sent.endswith('金属亮面怎么画')
    engine.dispose()
