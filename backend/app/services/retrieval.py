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
    def __init__(self, engine, provider_getter, *, mode='lexical', timeout=10):
        if mode not in ('lexical', 'hybrid'):
            raise ValueError('RETRIEVAL_MODE must be lexical or hybrid')
        self.engine = engine
        self.provider_getter = provider_getter
        self.mode = mode
        self.timeout = timeout

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
        selected = strategy or ('hybrid' if self.mode == 'hybrid' else 'bm25')
        if selected not in ('bm25', 'hybrid') or type(k) is not int or not 1 <= k <= 20:
            raise ValueError('Invalid retrieval strategy or result limit')
        started = perf_counter()
        prepared = (
            await self.prepare_query(query if semantic_query is None else semantic_query)
            if selected == 'hybrid'
            else None
        )
        result = await asyncio.to_thread(self._search, query, k, selected, prepared)
        result = await asyncio.to_thread(self.revalidate, result)
        result.diagnostics['latency_ms'] = round((perf_counter() - started) * 1000, 2)
        if result.diagnostics.get('fallback_reason'):
            LOGGER.warning(
                'retrieval_fallback reason=%s model=%s',
                result.diagnostics['fallback_reason'],
                result.diagnostics['model'],
            )
        return result

    def revalidate(self, result):
        with Session(self.engine) as db:
            hits, refs = repository.revalidate(db, result.hits, result.evidence_refs)
            return replace(result, hits=hits, evidence_refs=refs)
