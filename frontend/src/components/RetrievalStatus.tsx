import type { RetrievalMetadata } from '../retrievalMetadata'
const fallbackLabels: Record<string, string> = {
  empty_query: '查询为空',
  unconfigured: '未配置向量服务',
  unsupported_model: '当前向量模型不受支持',
  no_ready_vectors: '没有可用的匹配向量',
  timeout: '查询向量超时',
  provider_error: '向量服务请求失败',
  invalid_vector: '向量服务返回无效结果',
}
const strategyLabels: Record<string, string> = {
  bm25: 'BM25 词法检索',
  hybrid: '混合检索',
  rerank: '混合检索 + 重排',
}
const rerankFallbackLabels: Record<string, string> = {
  unconfigured: '未配置重排服务',
  timeout: '重排服务超时',
  provider_error: '重排服务请求失败',
}
export default function RetrievalStatus({
  metadata,
}: {
  metadata: RetrievalMetadata
}) {
  return (
    <div className="evaluation-note">
      <p>
        实际策略：
        {strategyLabels[metadata.effective_strategy] ?? 'BM25 词法检索'} ·{' '}
        {metadata.version}
      </p>
      {metadata.fallback_reason && (
        <p>
          已降级为词法检索：
          {fallbackLabels[metadata.fallback_reason] || '向量服务暂不可用'}
          。本次未完成混合检索。
        </p>
      )}
      <p>
        词法候选 {metadata.lexical_candidates} · 向量候选{' '}
        {metadata.vector_candidates}
        {metadata.model ? ` · 模型 ${metadata.model}` : ''}
        {metadata.dimensions ? ` / ${metadata.dimensions} 维` : ''}
      </p>
      {metadata.latency_ms !== undefined && (
        <p>
          总耗时 {metadata.latency_ms.toFixed(1)} ms
          {metadata.embedding_ms !== undefined
            ? ` · 查询向量 ${metadata.embedding_ms.toFixed(1)} ms`
            : ''}
        </p>
      )}
      {metadata.rerank_fallback_reason && (
        <p>
          未完成重排：
          {rerankFallbackLabels[metadata.rerank_fallback_reason] ||
            '重排服务暂不可用'}
          。结果按融合排序显示，未按阈值过滤。
        </p>
      )}
      {metadata.effective_strategy === 'rerank' && (
        <p>
          重排模型 {metadata.rerank_model} · 阈值 {metadata.rerank_min_score} ·
          过滤 {metadata.rerank_dropped ?? 0} 条
          {metadata.rerank_ms !== undefined
            ? ` · 重排 ${metadata.rerank_ms.toFixed(1)} ms`
            : ''}
        </p>
      )}
      {metadata.query_truncated && <p>语义查询已按长度上限截取。</p>}
    </div>
  )
}
