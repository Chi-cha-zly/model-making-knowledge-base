"""Main agent + research sub-agent: evidence integrity, limits and isolation."""

import asyncio
import json
from contextlib import asynccontextmanager

from agent_fakes import call, scripted
from langchain_core.messages import AIMessage, ToolMessage

from app.agents import knowledge
from app.agents.knowledge import (
    EvidenceCollector,
    build_main_agent,
    final_citations,
    stream_answer,
    unknown_citations,
)
from app.retrieval_types import EvidenceRef, RetrievalResult


def hit(key, title='标题', excerpt='摘录'):
    kind, id = key.split(':')
    return {
        'key': key,
        'type': kind,
        'id': int(id),
        'title': title,
        'excerpt': excerpt,
        'source_ids': [1],
        'sources': [{'id': 1, 'title': '来源', 'url': None}],
        'rank': 1,
        'score': 0.9,
        'reasons': [],
        'attachment_ids': [],
    }


class FakeRetrieval:
    engine = None

    def __init__(self, library, withdrawn=()):
        self.library, self.withdrawn, self.queries = library, set(withdrawn), []

    async def search(self, query, k=6, **kwargs):
        self.queries.append(query)
        hits = [hit(key) for key in self.library.get(query, [])]
        refs = {h['key']: EvidenceRef(h['id'], 1, ()) for h in hits}
        return RetrievalResult(hits, 'rerank', 'rerank', 'v', {}, refs)

    def revalidate(self, result):
        hits = [h for h in result.hits if h['key'] not in self.withdrawn]
        return RetrievalResult(
            hits,
            result.requested_strategy,
            result.effective_strategy,
            result.version,
            result.diagnostics,
            result.evidence_refs,
        )


def report(covered, missing=(), summary='找到了资料'):
    return call(
        'EvidenceReport',
        {
            'covered': [{'aspect': a, 'evidence_keys': keys} for a, keys in covered],
            'missing': list(missing),
            'summary': summary,
        },
        id='report',
    )


async def collect(agent, text):
    return ''.join(
        [chunk async for chunk in stream_answer(agent, [{'role': 'user', 'content': text}])]
    )


def test_research_flow_streams_answer_and_keeps_subagent_context_isolated():
    retrieval = FakeRetrieval({'NMM 原理': ['technique:7'], '金色步骤': ['technique:32']})
    research_model = scripted(
        call('search_library', {'query': 'NMM 原理'}, id='s1'),
        call('search_library', {'query': '金色步骤'}, id='s2'),
        report([('原理', ['technique:7']), ('步骤', ['technique:32', 'technique:999'])]),
    )
    main_model = scripted(
        call('research', {'task': '金色 NMM 怎么画'}),
        AIMessage(content='先铺底色 [technique:32]，原理见 [technique:7]。'),
    )
    collector = EvidenceCollector()
    agent = build_main_agent(main_model, research_model, retrieval, collector, '系统规则')
    answer = asyncio.run(collect(agent, '金色 NMM 怎么画？'))

    assert answer == '先铺底色 [technique:32]，原理见 [technique:7]。'
    assert retrieval.queries == ['NMM 原理', '金色步骤']
    assert [step['kind'] for step in collector.trace] == ['search', 'search', 'research']
    # The main model sees only the research observation, never the search turns.
    second_prompt = main_model.seen[1]
    tool_messages = [m for m in second_prompt if isinstance(m, ToolMessage)]
    observation = json.loads(tool_messages[0].content)
    assert all('search_library' not in str(m.content) for m in second_prompt)
    # A key the sub-agent never retrieved is dropped from the report.
    assert observation['covered'][1]['evidence_keys'] == ['technique:32']
    assert [e['key'] for e in observation['evidence']] == ['technique:7', 'technique:32']
    citations = final_citations(retrieval, collector, answer)
    assert [(c['type'], c['id']) for c in citations] == [('technique', 7), ('technique', 32)]
    assert unknown_citations(answer, citations) == []


def test_no_evidence_is_reported_missing_and_withdrawn_hits_are_not_cited():
    retrieval = FakeRetrieval({'税率': [], 'NMM': ['technique:7']}, withdrawn={'technique:7'})
    research_model = scripted(
        call('search_library', {'query': '税率'}, id='s1'),
        call('search_library', {'query': 'NMM'}, id='s2'),
        report([('税率', [])], missing=['澳洲税率'], summary='没有相关资料'),
    )
    main_model = scripted(
        call('research', {'task': '澳洲税率'}),
        AIMessage(content='资料库中没有依据 [technique:7]。'),
    )
    collector = EvidenceCollector()
    agent = build_main_agent(main_model, research_model, retrieval, collector, '规则')
    answer = asyncio.run(collect(agent, '澳洲税率？'))
    observation = json.loads(
        [m for m in main_model.seen[1] if isinstance(m, ToolMessage)][0].content
    )
    assert observation['covered'] == [] and '澳洲税率' in observation['missing']
    assert '税率' in observation['missing']
    citations = final_citations(retrieval, collector, answer)
    assert citations == []
    assert unknown_citations(answer, citations) == ['technique:7']


def test_search_limit_is_enforced_by_middleware_not_by_the_model():
    retrieval = FakeRetrieval({})
    research_model = scripted(
        *[call('search_library', {'query': f'q{i}'}, id=f's{i}') for i in range(6)],
        report([], missing=['全部']),
    )
    main_model = scripted(call('research', {'task': '任务'}), AIMessage(content='没有依据。'))
    collector = EvidenceCollector()
    agent = build_main_agent(main_model, research_model, retrieval, collector, '规则')
    asyncio.run(collect(agent, '问题'))
    assert len(retrieval.queries) == knowledge.MAX_SEARCHES


def test_read_technique_only_reads_evidence_already_found(monkeypatch):
    retrieval = FakeRetrieval({'NMM': ['technique:7']})
    reads = []

    def fake_text(db, key, limit=6000):
        reads.append(key)
        return '完整正文'

    monkeypatch.setattr(knowledge, 'document_text', fake_text)
    monkeypatch.setattr(knowledge, 'Session', _NullSession)
    research_model = scripted(
        call('search_library', {'query': 'NMM'}, id='s1'), report([('原理', ['technique:7'])])
    )
    main_model = scripted(
        call('read_technique', {'key': 'technique:1'}, id='r0'),
        call('research', {'task': 'NMM'}, id='r1'),
        call('read_technique', {'key': 'technique:7'}, id='r2'),
        AIMessage(content='答案'),
    )
    collector = EvidenceCollector()
    agent = build_main_agent(main_model, research_model, retrieval, collector, '规则')
    asyncio.run(collect(agent, '问题'))
    refused = [m for m in main_model.seen[1] if isinstance(m, ToolMessage)][-1]
    assert '只能读取' in refused.content
    assert reads == ['technique:7']


class _NullSession:
    def __init__(self, *args):
        pass

    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def test_chat_service_agent_mode_persists_citations_and_trace():
    from pg_test_support import test_database_url

    from app.chat import ChatService, TurnInput
    from app.db import create_db_engine

    retrieval = FakeRetrieval({'NMM': ['technique:7']})

    class Provider:
        configured = True
        model = 'fake'

        @asynccontextmanager
        async def agent_models(self):
            yield (
                scripted(call('research', {'task': 'NMM'}), AIMessage(content='见 [technique:7]')),
                scripted(
                    call('search_library', {'query': 'NMM'}, id='s1'),
                    report([('原理', ['technique:7'])]),
                ),
            )

    async def scenario():
        engine = create_db_engine(test_database_url('agent chat'))
        retrieval.engine = engine
        service = ChatService(engine, Provider(), retrieval=retrieval, agent_mode=True)
        cid = service.create_conversation('agent')['id']
        try:
            async with service.lock:
                run = await service.create_run(cid, TurnInput(content='NMM 是什么', request_id='a'))
            await asyncio.wait_for(service.tasks[run['id']], 5)
            snapshot = service.snapshot(run['id'])
            assert snapshot['status'] == 'completed', snapshot['error']
            assert snapshot['content'] == '见 [technique:7]'
            assert [c['id'] for c in snapshot['citations']] == [7]
            steps = service.repository.trace(run['id'])['steps']
            assert [s['kind'] for s in steps] == ['search', 'research']
        finally:
            engine.dispose()

    asyncio.run(scenario())


def test_only_relied_on_or_cited_evidence_becomes_citations():
    retrieval = FakeRetrieval({'NMM': ['technique:7', 'technique:8', 'technique:9']})
    research_model = scripted(
        call('search_library', {'query': 'NMM'}, id='s1'), report([('原理', ['technique:7'])])
    )
    main_model = scripted(
        call('research', {'task': 'NMM'}),
        AIMessage(content='原理 [technique:7]，另见 [technique:9]。'),
    )
    collector = EvidenceCollector()
    agent = build_main_agent(main_model, research_model, retrieval, collector, '规则')
    answer = asyncio.run(collect(agent, 'NMM？'))
    citations = final_citations(retrieval, collector, answer)
    assert [c['id'] for c in citations] == [7, 9]


def test_preamble_before_a_tool_call_is_not_part_of_the_answer():
    retrieval = FakeRetrieval({'NMM': ['technique:7']})
    research_model = scripted(
        call('search_library', {'query': 'NMM'}, id='s1'), report([('原理', ['technique:7'])])
    )
    preamble = AIMessage(
        content='我先查一下资料。',
        tool_calls=[{'name': 'research', 'args': {'task': 'NMM'}, 'id': 'c1'}],
    )
    main_model = scripted(preamble, AIMessage(content='答案 [technique:7]'))
    agent = build_main_agent(main_model, research_model, retrieval, EvidenceCollector(), '规则')
    assert asyncio.run(collect(agent, 'NMM？')) == '答案 [technique:7]'


def test_fallback_report_only_covers_hits_from_its_own_research_call():
    retrieval = FakeRetrieval({'a': ['technique:1'], 'b': ['technique:2']})
    research_model = scripted(
        call('search_library', {'query': 'a'}, id='s1'),
        report([('a', ['technique:1'])]),
        # Second research call never produces a report: the model-call limit ends it.
        *[call('search_library', {'query': 'b'}, id=f't{i}') for i in range(8)],
    )
    main_model = scripted(
        call('research', {'task': 'a'}, id='r1'),
        call('research', {'task': 'b'}, id='r2'),
        AIMessage(content='完成'),
    )
    collector = EvidenceCollector()
    agent = build_main_agent(main_model, research_model, retrieval, collector, '规则')
    asyncio.run(collect(agent, '问题'))
    observations = [json.loads(m.content) for m in main_model.seen[2] if isinstance(m, ToolMessage)]
    assert [e['key'] for e in observations[1]['evidence']] == ['technique:2']


def test_stopping_an_agent_run_keeps_trace_and_verified_citations():
    from pg_test_support import test_database_url

    from app.chat import ChatService, TurnInput
    from app.db import create_db_engine

    gate = asyncio.Event()

    class SlowRetrieval(FakeRetrieval):
        async def search(self, query, k=6, **kwargs):
            result = await super().search(query, k, **kwargs)
            if query == 'slow':
                gate.set()
                await asyncio.sleep(3600)
            return result

    retrieval = SlowRetrieval({'NMM': ['technique:7'], 'slow': []})

    class Provider:
        configured = True
        model = 'fake'

        @asynccontextmanager
        async def agent_models(self):
            yield (
                scripted(
                    call('read_technique', {'key': 'technique:7'}, id='x'),
                    call('research', {'task': 'NMM'}, id='r1'),
                    call('research', {'task': 'slow'}, id='r2'),
                ),
                scripted(
                    call('search_library', {'query': 'NMM'}, id='s1'),
                    report([('原理', ['technique:7'])]),
                    call('search_library', {'query': 'slow'}, id='s2'),
                ),
            )

    async def scenario():
        engine = create_db_engine(test_database_url('agent stop'))
        retrieval.engine = engine
        service = ChatService(engine, Provider(), retrieval=retrieval, agent_mode=True)
        cid = service.create_conversation('agent')['id']
        try:
            async with service.lock:
                run = await service.create_run(cid, TurnInput(content='NMM', request_id='a'))
            await asyncio.wait_for(gate.wait(), 5)
            async with service.lock:
                stopped = await service.stop(run['id'])
            assert stopped['status'] == 'stopped'
            assert [c['id'] for c in stopped['citations']] == [7]
            kinds = [s['kind'] for s in service.repository.trace(run['id'])['steps']]
            # The cancelled search never completed, so it left no step.
            assert kinds == ['search', 'research']
            assert run['id'] not in service.tasks
        finally:
            engine.dispose()

    asyncio.run(scenario())
