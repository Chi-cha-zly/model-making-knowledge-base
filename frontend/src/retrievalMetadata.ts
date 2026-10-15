export type RetrievalMetadata = {
  requested_strategy: 'bm25' | 'hybrid' | 'rerank'
  effective_strategy: 'bm25' | 'hybrid' | 'rerank'
  version: string
  fallback_reason: string | null
  query_truncated?: boolean
  lexical_candidates: number
  vector_candidates: number
  model?: string | null
  dimensions?: number | null
  embedding_ms?: number
  latency_ms?: number
  rerank_model?: string | null
  rerank_min_score?: number
  rerank_ms?: number
  rerank_dropped?: number
  rerank_fallback_reason?: string | null
}
