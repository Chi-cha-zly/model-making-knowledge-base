export type RetrievalMetadata = {
  requested_strategy: 'bm25' | 'hybrid'
  effective_strategy: 'bm25' | 'hybrid'
  version: string
  fallback_reason: string | null
  query_truncated?: boolean
  lexical_candidates: number
  vector_candidates: number
  model?: string | null
  dimensions?: number | null
  embedding_ms?: number
  latency_ms?: number
}
