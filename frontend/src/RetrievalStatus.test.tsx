import { afterEach, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import RetrievalStatus from './components/RetrievalStatus'
import type { RetrievalMetadata } from './retrievalMetadata'

afterEach(cleanup)

const base: RetrievalMetadata = {
  requested_strategy: 'rerank',
  effective_strategy: 'rerank',
  version: 'hybrid-rrf60-rerank-1',
  fallback_reason: null,
  lexical_candidates: 3,
  vector_candidates: 20,
  rerank_model: 'qwen3-rerank',
  rerank_min_score: 0.35,
  rerank_ms: 1800,
  rerank_dropped: 17,
  rerank_fallback_reason: null,
}

it('labels reranked results with model, threshold and dropped count', () => {
  render(<RetrievalStatus metadata={base} />)
  expect(screen.getByText(/实际策略：混合检索 \+ 重排/)).toBeInTheDocument()
  expect(
    screen.getByText(/重排模型 qwen3-rerank · 阈值 0.35 · 过滤 17 条/),
  ).toBeInTheDocument()
})

it('explains an unfinished rerank instead of claiming filtered results', () => {
  render(
    <RetrievalStatus
      metadata={{
        ...base,
        effective_strategy: 'hybrid',
        rerank_fallback_reason: 'timeout',
      }}
    />,
  )
  expect(screen.getByText(/实际策略：混合检索 ·/)).toBeInTheDocument()
  expect(screen.getByText(/未完成重排：重排服务超时/)).toBeInTheDocument()
  expect(screen.queryByText(/重排模型/)).not.toBeInTheDocument()
})
