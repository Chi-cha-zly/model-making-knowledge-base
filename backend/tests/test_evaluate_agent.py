"""Agent evaluation harness: scoring, call counting, budget and parallel search."""

import asyncio
from contextlib import asynccontextmanager
from time import perf_counter

from agent_fakes import call, scripted
from langchain_core.messages import AIMessage
from test_agents import FakeRetrieval, report

from app.agents.knowledge import AgentOptions
from scripts import evaluate_agent


class Provider:
    def __init__(self, *scripts):
        self.scripts = list(scripts)

    @asynccontextmanager
    async def agent_models(self, research_reasoning=None):
        yield self.scripts.pop(0)


def answerable():
    return {'id': 'a', 'kind': 'answerable', 'query': 'NMM', 'relevant': ['technique:7']}


def test_run_case_counts_nested_model_calls_and_scores_grounding():
    retrieval = FakeRetrieval({'NMM': ['technique:7', 'technique:8']})
    provider = Provider(
        (
            scripted(call('research', {'task': 'NMM'}), AIMessage(content='见 [technique:7]')),
            scripted(
                call('search_library', {'query': 'NMM'}, id='s'),
                report([('原理', ['technique:7'])]),
            ),
        )
    )
    row = asyncio.run(
        evaluate_agent.run_case(answerable(), provider, retrieval, '规则', AgentOptions())
    )
    # 2 main-model calls + 2 sub-agent calls; one search = embed + rerank.
    assert row['model_calls'] == 4 and row['paid_calls'] == 6
    assert row['tool_ok'] and row['grounded'] and row['citation_precision_lb'] == 1.0
    assert row['invalid_citations'] == []


def test_scoring_for_refusal_and_chat():
    assert evaluate_agent.score_case(
        {'kind': 'out_of_domain', 'relevant': []}, '没有依据', [], [{'kind': 'research'}]
    )['refused']
    chat = evaluate_agent.score_case({'kind': 'chat', 'relevant': []}, '你好', [], [])
    assert chat['tool_ok'] and chat['refused']
    called = evaluate_agent.score_case(
        {'kind': 'chat', 'relevant': []}, '你好', [], [{'kind': 'research'}]
    )
    assert not called['tool_ok']


def test_parallel_searches_in_one_turn_run_concurrently():
    class SlowRetrieval(FakeRetrieval):
        async def search(self, query, k=6, **kwargs):
            await asyncio.sleep(0.3)
            return await super().search(query, k, **kwargs)

    retrieval = SlowRetrieval({'a': ['technique:1'], 'b': ['technique:2']})
    both = AIMessage(
        content='',
        tool_calls=[
            {'name': 'search_library', 'args': {'query': 'a'}, 'id': 's1'},
            {'name': 'search_library', 'args': {'query': 'b'}, 'id': 's2'},
        ],
    )
    provider = Provider(
        (
            scripted(call('research', {'task': 'ab'}), AIMessage(content='完成')),
            scripted(both, report([('a', ['technique:1']), ('b', ['technique:2'])])),
        )
    )
    started = perf_counter()
    row = asyncio.run(
        evaluate_agent.run_case(
            answerable(), provider, retrieval, '规则', AgentOptions(parallel_search=True)
        )
    )
    assert row['searches'] == 2
    assert perf_counter() - started < 0.55


def test_budget_stops_before_a_case_could_overspend():
    provider = Provider()
    rows, status = asyncio.run(
        evaluate_agent.evaluate(
            [answerable()], provider, FakeRetrieval({}), '规则', AgentOptions(), 10
        )
    )
    assert rows == [] and status == 'budget_stopped'


def test_direct_search_counts_as_tool_use():
    row = evaluate_agent.score_case(
        answerable(),
        '见 [technique:7]',
        [{'type': 'technique', 'id': 7}],
        [{'kind': 'search', 'source': 'main', 'keys': ['technique:7']}],
    )
    assert row['tool_ok'] and row['grounded']
