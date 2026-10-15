"""Explicit agent evaluation: tool use, refusal, grounding, latency and call counts.

Dry-run by default. --apply sends each case to the configured chat model and the
retrieval services; a running paid-call count aborts before the budget is exceeded.
"""

import argparse
import asyncio
import json
import statistics
from dataclasses import asdict
from pathlib import Path
from time import perf_counter

from langchain_core.callbacks import AsyncCallbackHandler

from app.agents.knowledge import (
    AgentOptions,
    EvidenceCollector,
    build_main_agent,
    final_citations,
    stream_answer,
    unknown_citations,
)
from scripts.import_nmm import validate_report_path, write_report

# Upper bound of paid calls one case may still make (model calls + embed/rerank per search).
CASE_RESERVE = 25


class CallCounter(AsyncCallbackHandler):
    """Counts and times chat-model calls across the main agent and the sub-agent."""

    def __init__(self):
        self.model_calls = 0
        self.started = {}
        self.timings = []

    async def on_chat_model_start(self, serialized, messages, *, run_id, metadata=None, **kw):
        self.model_calls += 1
        self.started[run_id] = (perf_counter(), (metadata or {}).get('lc_agent_name', '?'))

    async def on_llm_end(self, response, *, run_id, **kwargs):
        if run_id in self.started:
            began, agent = self.started.pop(run_id)
            self.timings.append({'agent': agent, 'seconds': round(perf_counter() - began, 2)})


def score_case(case, answer, citations, trace):
    """Pure per-case metrics; labelled keys are a lower bound of what is relevant."""
    cited = [f'{c["type"]}:{c["id"]}' for c in citations]
    relevant = set(case['relevant'])
    researched = any(step['kind'] == 'research' for step in trace)
    result = {
        'researched': researched,
        'searches': sum(step['kind'] == 'search' for step in trace),
        'cited': cited,
        'invalid_citations': unknown_citations(answer, citations),
    }
    if case['kind'] == 'answerable':
        result['tool_ok'] = researched
        result['grounded'] = bool(relevant & set(cited))
        result['citation_precision_lb'] = len(relevant & set(cited)) / len(cited) if cited else 0.0
    elif case['kind'] == 'out_of_domain':
        result['refused'] = not cited
    else:
        result['tool_ok'] = not researched
        result['refused'] = not cited
    return result


def summarize(rows):
    def rate(kind, key):
        values = [row[key] for row in rows if row['kind'] == kind and key in row]
        return round(sum(values) / len(values), 3) if values else None

    latencies = [row['seconds'] for row in rows]
    answerable = [r for r in rows if r['kind'] == 'answerable']
    return {
        'cases': len(rows),
        'answerable_tool_use': rate('answerable', 'tool_ok'),
        'answerable_grounded': rate('answerable', 'grounded'),
        'citation_precision_lb': round(
            statistics.mean(r['citation_precision_lb'] for r in answerable), 3
        )
        if answerable
        else None,
        'out_of_domain_refused': rate('out_of_domain', 'refused'),
        'chat_no_tool': rate('chat', 'tool_ok'),
        'invalid_citation_cases': sum(bool(r['invalid_citations']) for r in rows),
        'median_seconds': round(statistics.median(latencies), 1) if latencies else None,
        'max_seconds': max(latencies) if latencies else None,
        'mean_searches': round(statistics.mean(r['searches'] for r in rows), 2) if rows else None,
        'paid_calls': sum(r['paid_calls'] for r in rows),
    }


async def run_case(case, provider, retrieval, system_prompt, options):
    collector, counter = EvidenceCollector(), CallCounter()
    started = perf_counter()
    async with provider.agent_models(options.research_reasoning) as (main, research):
        agent = build_main_agent(main, research, retrieval, collector, system_prompt, options)
        messages = [{'role': 'user', 'content': case['query']}]
        answer = ''.join(
            [chunk async for chunk in stream_answer(agent, messages, {'callbacks': [counter]})]
        )
    seconds = round(perf_counter() - started, 1)
    citations = await asyncio.to_thread(final_citations, retrieval, collector, answer)
    searches = sum(step['kind'] == 'search' for step in collector.trace)
    return {
        'id': case['id'],
        'kind': case['kind'],
        'query': case['query'],
        'seconds': seconds,
        'model_calls': counter.model_calls,
        'model_timings': counter.timings,
        # Each rerank-mode search makes one embedding and one rerank call.
        'paid_calls': counter.model_calls + 2 * searches,
        'answer': answer,
        'trace': collector.trace,
        **score_case(case, answer, citations, collector.trace),
    }


async def evaluate(cases, provider, retrieval, system_prompt, options, max_calls, on_case=None):
    rows, spent = [], 0
    for case in cases:
        if spent + CASE_RESERVE > max_calls:
            return rows, 'budget_stopped'
        row = await run_case(case, provider, retrieval, system_prompt, options)
        rows.append(row)
        spent += row['paid_calls']
        if on_case:
            on_case(rows)
    return rows, 'completed'


def main(argv=None, *, provider=None, retrieval=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-url', required=True)
    parser.add_argument('--dataset', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--storage-dir', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--max-calls', type=int, default=0)
    parser.add_argument('--max-searches', type=int, default=4)
    parser.add_argument('--parallel-search', action='store_true')
    parser.add_argument('--direct-search', action='store_true')
    parser.add_argument('--research-reasoning', default=None)
    parser.add_argument('--rule-report', action='store_true')
    parser.add_argument('--label', default='')
    args = parser.parse_args(argv)
    output = validate_report_path(args.report, args.storage_dir)
    cases = json.loads(args.dataset.read_text())['cases']
    options = AgentOptions(
        max_searches=args.max_searches,
        parallel_search=args.parallel_search,
        direct_search=args.direct_search,
        research_reasoning=args.research_reasoning,
        rule_report=args.rule_report,
    )
    result = {
        'status': 'dry_run',
        'label': args.label,
        'options': asdict(options),
        'cases': len(cases),
        'max_calls': args.max_calls,
    }
    if args.apply:
        from app.prompts import SYSTEM

        if provider is None or retrieval is None:
            provider, retrieval = live_services(args.database_url)

        def save(rows):
            write_report(output, {**result, 'status': 'running', 'rows': rows})

        rows, status = asyncio.run(
            evaluate(cases, provider, retrieval, SYSTEM, options, args.max_calls, save)
        )
        result.update(status=status, summary=summarize(rows), rows=rows)
    write_report(output, result)
    print(json.dumps({k: result[k] for k in ('status', 'label')}, ensure_ascii=False))
    return 0


def live_services(database_url):
    """Real chat model from the project settings; retrieval in rerank mode from env."""
    import os

    from app.db import create_db_engine
    from app.llm import LangChainProvider
    from app.rerank import DashScopeReranker
    from app.runtime_config import read_settings
    from app.services.retrieval import RetrievalService

    provider = LangChainProvider(read_settings())
    reranker = DashScopeReranker(os.environ)
    retrieval = RetrievalService(
        create_db_engine(database_url),
        lambda: provider,
        mode='rerank',
        reranker_getter=lambda: reranker,
        rerank_min_score=float(os.environ.get('RERANK_MIN_SCORE') or 0.35),
    )
    return provider, retrieval


if __name__ == '__main__':
    raise SystemExit(main())
