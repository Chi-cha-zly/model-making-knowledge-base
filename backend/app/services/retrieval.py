"""Async query encoding followed by bounded, authorized database retrieval."""

import asyncio
from dataclasses import dataclass, replace
import logging
from time import perf_counter

from sqlalchemy.orm import Session

from ..embedding_contract import EMBEDDING_DIMENSIONS, QUERY_MODELS, validate_vectors
from ..errors import ProviderError
from ..lexical import _normalize
from ..repositories import retrieval as repository
from ..retrieval import PUBLIC_FIELDS, _contains, _identifier_match, _sku_transposition, STOP_WORDS
from ..retrieval_types import RetrievalResult

LOGGER = logging.getLogger(__name__)
BM25_VERSION = 'indexed-bm25-2'
HYBRID_VERSION = 'hybrid-rrf60-rules-1'
RERANK_VERSION = 'hybrid-rrf60-rerank-1'
# Vector query failed, so only lexical candidates were reranked.
BM25_RERANK_VERSION = 'bm25-rerank-1'
STRATEGIES = ('bm25', 'hybrid', 'rerank')
# Exact identifier/alias hits stay even when a cross-encoder scores a bare code low.
PROTECTED_REASONS = ('编号规范化精确匹配', '别名精确匹配')


def bounded_query(query):
    """Same head+tail window prepare_query uses for over-long queries."""
    return query[:1500] + query[-500:] if len(query) > 2000 else query


def is_protected(hit):
    return any(reason.startswith(PROTECTED_REASONS) for reason in hit['reasons'])


def apply_threshold(scored_hits, threshold):
    """Keep reranked hits at or above ``threshold`` plus exact identifier/alias hits."""
    return [hit for hit in scored_hits if hit['score'] >= threshold or is_protected(hit)]


@dataclass(frozen=True)
class PreparedQuery:
    vector: list[float] | None
    model: str | None
    fallback_reason: str | None = None
    query_truncated: bool = False
    embedding_ms: float = 0


def rule_tier(document, query):
    normalized = _normalize(query)
    if _identifier_match(normalized, document.get('sku', '')):
        return 3, '编号规范化精确匹配'
    if any(
        _contains(normalized, _normalize(alias))
        for alias in document.get('aliases', [])
        if alias and _normalize(alias) not in STOP_WORDS
    ):
        return 2, '别名精确匹配'
    if _sku_transposition(normalized, document.get('sku', '')):
        return 1, '长编号相邻字符容错（需核对型号）'
    return 0, ''


class RetrievalService:
    def __init__(
        self,
        engine,
        provider_getter,
        *,
        mode='lexical',
        timeout=10,
        reranker_getter=None,
        rerank_min_score=0.0,
        rerank_candidates=20,
    ):
        if mode not in ('lexical', 'hybrid', 'rerank'):
            raise ValueError('RETRIEVAL_MODE must be lexical, hybrid or rerank')
        if not 0 <= rerank_min_score <= 1 or not 1 <= rerank_candidates <= 20:
            raise ValueError('Invalid rerank threshold or candidate count')
        self.engine = engine
        self.provider_getter = provider_getter
        self.mode = mode
        self.timeout = timeout
        self.reranker_getter = reranker_getter or (lambda: None)
        self.rerank_min_score = rerank_min_score
        self.rerank_candidates = rerank_candidates

    def _has_vectors(self, model):
        with Session(self.engine) as db:
            return repository.has_vectors(db, model)

    async def prepare_query(self, query):
        provider = self.provider_getter()
        model = getattr(provider, 'embedding_model', None)
        truncated = len(query) > 2000
        query = query[:1500] + query[-500:] if truncated else query
        if not query.strip():
            return PreparedQuery(None, model, 'empty_query', truncated)
        if not getattr(provider, 'embedding_configured', False):
            return PreparedQuery(None, model, 'unconfigured', truncated)
        if model not in QUERY_MODELS:
            return PreparedQuery(None, model, 'unsupported_model', truncated)
        if not await asyncio.to_thread(self._has_vectors, model):
            return PreparedQuery(None, model, 'no_ready_vectors', truncated)
        started = perf_counter()
        try:
            vector = await asyncio.wait_for(provider.embed_query(query), timeout=self.timeout)
        except TimeoutError:
            return PreparedQuery(
                None, model, 'timeout', truncated, round((perf_counter() - started) * 1000, 2)
            )
        except ProviderError:
            return PreparedQuery(
                None,
                model,
                'provider_error',
                truncated,
                round((perf_counter() - started) * 1000, 2),
            )
        try:
            validate_vectors([vector], 1)
        except ProviderError:
            return PreparedQuery(
                None,
                model,
                'invalid_vector',
                truncated,
                round((perf_counter() - started) * 1000, 2),
            )
        return PreparedQuery(
            vector,
            model,
            query_truncated=truncated,
            embedding_ms=round((perf_counter() - started) * 1000, 2),
        )

    def search_prepared(self, db, query, *, k=6, strategy='bm25', prepared=None):
        if type(k) is not int or not 1 <= k <= 20:
            raise ValueError('k must be between 1 and 20')
        if strategy not in ('bm25', 'hybrid'):
            raise ValueError('Unknown retrieval strategy')
        prepared = prepared or PreparedQuery(None, None, 'unconfigured')
        lexical, refs, lexical_chunks = repository.lexical_search(db, query, k=200)
        vector_chunks = []
        effective = 'bm25'
        hits = lexical[:k]
        if strategy == 'hybrid' and prepared.vector is not None:
            vector_chunks = repository.vector_candidates(db, prepared.vector, prepared.model)
            documents, hybrid_refs = repository.hydrate(
                db,
                list(dict.fromkeys(vector_chunks[:1] + lexical_chunks + vector_chunks)),
                query,
                semantic_ids=vector_chunks,
            )
            # Each document contributes at most once per route, irrespective of chunk count.
            vector_documents, _ = repository.hydrate(
                db, vector_chunks, query, semantic_ids=vector_chunks
            )
            lexical_ranks = {hit['key']: rank for rank, hit in enumerate(lexical, 1)}
            vector_ranks = {key: rank for rank, key in enumerate(vector_documents, 1)}
            scores = {
                key: sum(
                    1 / (60 + ranks[key]) for ranks in (lexical_ranks, vector_ranks) if key in ranks
                )
                for key in lexical_ranks.keys() | vector_ranks.keys()
            }
            lexical_by_key = {hit['key']: hit for hit in lexical}
            tiers = {key: rule_tier(documents[key], query) for key in scores if key in documents}
            ordered = sorted(
                tiers,
                key=lambda key: (
                    -tiers[key][0],
                    -scores[key],
                    -lexical_by_key.get(key, {}).get('score', 0),
                    key,
                ),
            )
            hits = []
            for key in ordered[:k]:
                doc = documents[key]
                reasons = []
                if key in lexical_ranks:
                    reasons.append(f'词法召回 #{lexical_ranks[key]}')
                    reasons.extend(lexical_by_key[key]['reasons'])
                if key in vector_ranks:
                    reasons.append(f'向量召回 #{vector_ranks[key]}（{prepared.model} / 1024维）')
                reasons.append('RRF 融合（常数60）；本地规则重排，分数不是概率')
                if tiers[key][1]:
                    reasons.append(tiers[key][1] + '（规则提升）')
                hits.append(
                    {
                        **{field: doc[field] for field in PUBLIC_FIELDS},
                        'attachment_ids': doc['attachment_ids'],
                        'rank': len(hits) + 1,
                        'score': round(scores[key], 8),
                        'reasons': reasons,
                    }
                )
            refs = hybrid_refs
            effective = 'hybrid'
        metadata = {
            'embedding_ms': prepared.embedding_ms if strategy == 'hybrid' else 0,
            'fallback_reason': prepared.fallback_reason if strategy == 'hybrid' else None,
            'query_truncated': prepared.query_truncated if strategy == 'hybrid' else False,
            'lexical_candidates': len(lexical_chunks),
            'vector_candidates': len(vector_chunks),
            'model': prepared.model if strategy == 'hybrid' else None,
            'dimensions': EMBEDDING_DIMENSIONS if effective == 'hybrid' else None,
        }
        hits, refs = repository.revalidate(db, hits, refs)
        return RetrievalResult(
            hits,
            strategy,
            effective,
            HYBRID_VERSION if effective == 'hybrid' else BM25_VERSION,
            metadata,
            refs,
        )

    def _search(self, query, k, strategy, prepared):
        with Session(self.engine) as db:
            return self.search_prepared(db, query, k=k, strategy=strategy, prepared=prepared)

    async def search(self, query, *, k=6, strategy=None, semantic_query=None):
        if not isinstance(query, str) or len(query) > 64000:
            raise ValueError('query must be a string of at most 64000 characters')
        selected = strategy or {'lexical': 'bm25'}.get(self.mode, self.mode)
        if selected not in STRATEGIES or type(k) is not int or not 1 <= k <= 20:
            raise ValueError('Invalid retrieval strategy or result limit')
        started = perf_counter()
        prepared = (
            await self.prepare_query(query if semantic_query is None else semantic_query)
            if selected != 'bm25'
            else None
        )
        base = 'hybrid' if selected == 'rerank' else selected
        limit = self.rerank_candidates if selected == 'rerank' else k
        result = await asyncio.to_thread(self._search, query, limit, base, prepared)
        if selected == 'rerank':
            # Chat joins several turns oldest-first; score against the current question.
            result = await self.rerank(
                semantic_query if semantic_query is not None else bounded_query(query),
                result,
                k,
            )
        result = await asyncio.to_thread(self.revalidate, result)
        result.diagnostics['latency_ms'] = round((perf_counter() - started) * 1000, 2)
        for key in ('fallback_reason', 'rerank_fallback_reason'):
            if result.diagnostics.get(key):
                LOGGER.warning('retrieval_fallback %s=%s', key, result.diagnostics[key])
        return result

    async def rerank(self, query, result, k, *, min_score=None):
        """Rescore fused candidates; drop those below the calibrated threshold.

        A failed rerank never hides evidence: the fused order is kept, unfiltered,
        and the fallback reason is reported.
        """
        threshold = self.rerank_min_score if min_score is None else min_score
        reranker = self.reranker_getter()
        diagnostics = {
            **result.diagnostics,
            'rerank_base': result.effective_strategy,
            'rerank_model': getattr(reranker, 'model', None),
            'rerank_min_score': threshold,
            'rerank_candidates': len(result.hits),
            'rerank_ms': 0,
            'rerank_dropped': 0,
            'rerank_fallback_reason': None,
        }
        hits = result.hits
        if not hits:
            # Nothing recalled is already the "no evidence" answer.
            return replace(
                result,
                requested_strategy='rerank',
                effective_strategy='rerank',
                version=RERANK_VERSION,
                diagnostics=diagnostics,
            )
        if reranker is None or not getattr(reranker, 'configured', False):
            diagnostics['rerank_fallback_reason'] = 'unconfigured'
            return replace(
                result,
                hits=self._renumber(hits[:k]),
                requested_strategy='rerank',
                diagnostics=diagnostics,
            )
        started = perf_counter()
        try:
            scores = await asyncio.wait_for(
                reranker.rerank(query, [hit['title'] + '\n' + hit['excerpt'] for hit in hits]),
                timeout=self.timeout,
            )
        except (TimeoutError, ProviderError) as error:
            diagnostics['rerank_ms'] = round((perf_counter() - started) * 1000, 2)
            diagnostics['rerank_fallback_reason'] = (
                'timeout' if isinstance(error, TimeoutError) else 'provider_error'
            )
            return replace(
                result,
                hits=self._renumber(hits[:k]),
                requested_strategy='rerank',
                diagnostics=diagnostics,
            )
        diagnostics['rerank_ms'] = round((perf_counter() - started) * 1000, 2)
        scored = sorted(
            (
                {
                    **hit,
                    'score': round(score, 6),
                    'reasons': [*hit['reasons'], f'重排相关度 {score:.3f}（{reranker.model}）'],
                }
                for hit, score in zip(hits, scores)
            ),
            key=lambda hit: (-is_protected(hit), -hit['score']),
        )
        kept = apply_threshold(scored, threshold)
        diagnostics['rerank_dropped'] = len(scored) - len(kept)
        return replace(
            result,
            hits=self._renumber(kept[:k]),
            requested_strategy='rerank',
            effective_strategy='rerank',
            version=RERANK_VERSION
            if result.effective_strategy == 'hybrid'
            else BM25_RERANK_VERSION,
            diagnostics=diagnostics,
        )

    @staticmethod
    def _renumber(hits):
        return [{**hit, 'rank': rank} for rank, hit in enumerate(hits, 1)]

    def revalidate(self, result):
        with Session(self.engine) as db:
            hits, refs = repository.revalidate(db, result.hits, result.evidence_refs)
            return replace(result, hits=hits, evidence_refs=refs)
