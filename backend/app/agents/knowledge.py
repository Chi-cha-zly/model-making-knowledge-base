"""Main agent + retrieval sub-agent for chat.

Design:
- The sub-agent owns query decomposition and repeated search. It runs in its own
  message history and returns only a structured evidence report, so search
  attempts never enter the conversation context.
- Every hit any tool sees goes through ``EvidenceCollector``. Citations are taken
  from the collector and revalidated at the end, never from model text.
- Limits are enforced by middleware, not by asking the model to stop.
"""

import json
import re
from contextvars import ContextVar
from time import perf_counter
from dataclasses import dataclass, field

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain.agents.structured_output import ToolStrategy
from langchain.tools import tool
from langchain_core.messages import AIMessage, AIMessageChunk
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..chat_context import bounded_evidence
from ..repositories.retrieval import document_text
from ..retrieval_types import RetrievalResult
from .prompts import (
    DIRECT_SEARCH_RULE,
    MAIN_AGENT_RULES,
    PARALLEL_SEARCH_RULE,
    RESEARCH_PROMPT,
    SINGLE_TURN_RESEARCH_PROMPT,
)

MAIN_NAME = 'knowledge_agent'
RESEARCH_NAME = 'research_agent'
MAX_SEARCHES = 4
MAX_RESEARCH_CALLS = 3
MAX_MAIN_MODEL_CALLS = 6
CITATION = re.compile(r'\[(technique|product):(\d+)\]')
# Search steps of the research call currently running in this task. Concurrent tool
# calls run in separate tasks with copied contexts, so their steps never mix.
_research_steps: ContextVar[list | None] = ContextVar('research_steps', default=None)


@dataclass(frozen=True)
class AgentOptions:
    """Tunable cost/latency knobs, compared by scripts/evaluate_agent.py."""

    max_searches: int = MAX_SEARCHES
    parallel_search: bool = False
    # B: main agent may search directly for simple questions, skipping the sub-agent.
    direct_search: bool = False
    # C: reasoning effort for the sub-agent model only (e.g. 'none'); None keeps the default.
    research_reasoning: str | None = None
    # D: sub-agent issues all searches in one model call; the report is built by rules.
    rule_report: bool = False


class CoveredAspect(BaseModel):
    aspect: str = Field(description='已找到依据的方面')
    evidence_keys: list[str] = Field(description='支持该方面的证据编号，如 technique:7')


class EvidenceReport(BaseModel):
    covered: list[CoveredAspect] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list, description='没有找到依据的方面')
    summary: str = Field(default='', description='两三句话概括找到了什么')


@dataclass
class EvidenceCollector:
    """Every hit returned to any agent in this run, in first-seen order."""

    results: list[RetrievalResult] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)
    # Evidence the agents actually relied on; only these become citations.
    used: set = field(default_factory=set)
    # What the agent is doing right now, for live progress in the UI.
    activity: str = '正在思考…'

    def add(self, result):
        self.results.append(result)

    @property
    def hits(self):
        seen = {}
        for result in self.results:
            for hit in result.hits:
                seen.setdefault(hit['key'], hit)
        return seen

    def merged(self, keys=None):
        hits = [hit for key, hit in self.hits.items() if keys is None or key in keys]
        refs = {}
        for result in self.results:
            for key, ref in result.evidence_refs.items():
                refs.setdefault(key, ref)
        return RetrievalResult(hits, 'agent', 'agent', 'agent-1', {}, refs)

    def step(self, kind, **data):
        self.trace.append({'kind': kind, **data})


def format_hits(hits):
    if not hits:
        return '无结果：资料库中没有与该检索相关且足够可靠的资料。'
    return '\n\n'.join(f'[{hit["key"]}] {hit["title"]}\n{hit["excerpt"][:600]}' for hit in hits)


def build_research_agent(model, retrieval, collector, options=AgentOptions()):
    @tool
    async def search_library(query: str) -> str:
        """在模型制作知识库中检索资料。query 用简短具体的检索词。"""
        return await run_search(retrieval, collector, query)

    limit = ToolCallLimitMiddleware(tool_name='search_library', run_limit=options.max_searches)
    if options.rule_report:
        # One planning call; the tools then run and the next model call is refused.
        return create_agent(
            model,
            tools=[search_library],
            system_prompt=SINGLE_TURN_RESEARCH_PROMPT.replace(
                '最多 4 次', f'最多 {options.max_searches} 次'
            ),
            middleware=[limit, ModelCallLimitMiddleware(run_limit=1, exit_behavior='end')],
            name=RESEARCH_NAME,
        )
    return create_agent(
        model,
        tools=[search_library],
        system_prompt=RESEARCH_PROMPT.replace(
            '最多检索 4 次', f'最多检索 {options.max_searches} 次'
        )
        + (PARALLEL_SEARCH_RULE if options.parallel_search else ''),
        # Tool-based structured output works on any tool-calling endpoint.
        response_format=ToolStrategy(EvidenceReport),
        middleware=[
            limit,
            ModelCallLimitMiddleware(run_limit=options.max_searches + 2, exit_behavior='end'),
        ],
        name=RESEARCH_NAME,
    )


async def run_search(retrieval, collector, query, source='research'):
    collector.activity = f'正在检索：{query[:40]}'
    started = perf_counter()
    result = await retrieval.search(query[:500], k=5)
    collector.add(result)
    collector.step(
        'search',
        query=query[:500],
        strategy=result.effective_strategy,
        keys=[hit['key'] for hit in result.hits],
        ms=round((perf_counter() - started) * 1000),
        source=source,
    )
    own = _research_steps.get()
    if own is not None:
        own.append(collector.trace[-1])
    return format_hits(result.hits)


def rule_report(search_steps, task):
    """Report from search results alone: every query is an aspect."""
    covered = [
        CoveredAspect(aspect=step['query'], evidence_keys=step['keys'])
        for step in search_steps
        if step['keys']
    ]
    missing = [step['query'] for step in search_steps if not step['keys']]
    if not search_steps:
        missing = [task[:200]]
    found = len({key for step in search_steps for key in step['keys']})
    return EvidenceReport(
        covered=covered,
        missing=missing,
        summary=f'检索 {len(search_steps)} 次，找到 {found} 条资料（按检索词整理，未经模型归纳）。',
    )


def research_observation(report, collector, mark_used=True):
    """Drop evidence keys the sub-agent never actually retrieved."""
    known = collector.hits
    covered = []
    for item in report.covered:
        keys = [key for key in item.evidence_keys if key in known]
        if keys:
            covered.append({'aspect': item.aspect, 'evidence_keys': keys})
        else:
            report.missing.append(item.aspect)
    used = list(dict.fromkeys(key for item in covered for key in item['evidence_keys']))
    if mark_used:
        collector.used.update(used)
    return {
        'covered': covered,
        'missing': report.missing,
        'summary': report.summary,
        'evidence': [
            {'key': key, 'title': known[key]['title'], 'excerpt': known[key]['excerpt'][:800]}
            for key in used
        ],
    }


def build_main_agent(
    model, research_model, retrieval, collector, system_prompt, options=AgentOptions()
):
    research_agent = build_research_agent(research_model, retrieval, collector, options)

    @tool
    async def research(task: str) -> str:
        """让检索助手在知识库中查资料。task 写清需要核实的全部条件。"""
        collector.activity = f'检索助手正在规划：{task[:40]}'
        token = _research_steps.set([])
        try:
            result = await research_agent.ainvoke(
                {'messages': [{'role': 'user', 'content': task[:2000]}]}
            )
            searches = _research_steps.get()
        finally:
            _research_steps.reset(token)
        report = result.get('structured_response')
        if options.rule_report:
            report = rule_report(searches, task)
        elif not isinstance(report, EvidenceReport):
            # Limits reached before a report: keep what this call found, flag the rest.
            found = list(dict.fromkeys(key for step in searches for key in step['keys']))
            report = EvidenceReport(
                covered=[CoveredAspect(aspect=task[:200], evidence_keys=found)] if found else [],
                missing=[] if found else [task[:200]],
                summary='检索助手未完成结构化报告，以下为已检索到的资料。',
            )
        # Rule reports list every reranked hit; only cited ones should become citations.
        observation = research_observation(report, collector, mark_used=not options.rule_report)
        collector.step(
            'research',
            task=task[:500],
            searches=len(searches),
            covered=[item['aspect'] for item in observation['covered']],
            missing=observation['missing'],
        )
        collector.activity = '正在根据资料撰写回答'
        return json.dumps(observation, ensure_ascii=False)

    @tool
    async def quick_search(query: str) -> str:
        """直接检索一次资料库，适合只涉及一个技法、型号或材料的简单问题。"""
        text = await run_search(retrieval, collector, query, source='main')
        collector.activity = '正在根据资料撰写回答'
        return text

    @tool
    def read_technique(key: str) -> str:
        """读取一条已检索到的资料的完整正文。key 例如 technique:7。"""
        if key not in collector.hits:
            return '只能读取检索返回过的证据编号。'
        collector.activity = f'正在阅读：{collector.hits[key]["title"][:40]}'
        with Session(retrieval.engine) as db:
            text = document_text(db, key)
        collector.step('read', key=key, found=bool(text))
        if text:
            collector.used.add(key)
        return text or '该资料当前不可用（可能已撤回）。'

    tools = [research, read_technique] + ([quick_search] if options.direct_search else [])
    return create_agent(
        model,
        tools=tools,
        system_prompt=system_prompt
        + '\n'
        + MAIN_AGENT_RULES
        + (DIRECT_SEARCH_RULE if options.direct_search else ''),
        middleware=[
            ToolCallLimitMiddleware(tool_name='research', run_limit=MAX_RESEARCH_CALLS),
            ToolCallLimitMiddleware(tool_name='quick_search', run_limit=MAX_RESEARCH_CALLS),
            ToolCallLimitMiddleware(run_limit=MAX_RESEARCH_CALLS + 3),
            ModelCallLimitMiddleware(run_limit=MAX_MAIN_MODEL_CALLS, exit_behavior='end'),
        ],
        name=MAIN_NAME,
    )


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ''.join(block.get('text', '') for block in content if isinstance(block, dict))
    return ''


# Text a model writes before deciding to call a tool ("let me check…") is not answer
# text. Hold each message's opening until it is clearly an answer.
PREAMBLE_HOLD = 120


async def stream_answer(agent, messages, config=None):
    """Yield answer text from the main agent's own model node only."""
    held, message_id, released, discarded = '', None, False, False
    stream = agent.astream({'messages': messages}, config, stream_mode='messages')
    async for chunk, meta in stream:
        if meta.get('lc_agent_name') != MAIN_NAME or meta.get('langgraph_node') != 'model':
            continue
        if not isinstance(chunk, (AIMessage, AIMessageChunk)):
            continue
        if chunk.id != message_id:
            if held and not discarded:
                yield held
            held, message_id, released, discarded = '', chunk.id, False, False
        if chunk.tool_calls or getattr(chunk, 'tool_call_chunks', None):
            discarded = not released
            held = ''
            continue
        text = text_of(chunk.content)
        if not text or discarded:
            continue
        if released:
            yield text
            continue
        held += text
        if len(held) >= PREAMBLE_HOLD:
            yield held
            held, released = '', True
    if held and not discarded:
        yield held


def final_citations(retrieval, collector, answer=''):
    """Revalidated evidence the agent relied on or cited, in the existing citation shape."""
    cited = {f'{kind}:{id}' for kind, id in CITATION.findall(answer)} & set(collector.hits)
    keys = collector.used | cited
    if not keys:
        return []
    return bounded_evidence(retrieval.revalidate(collector.merged(keys)).hits)


def unknown_citations(text, citations):
    allowed = {(c['type'], c['id']) for c in citations}
    return sorted(
        {f'{kind}:{id}' for kind, id in CITATION.findall(text) if (kind, int(id)) not in allowed}
    )
