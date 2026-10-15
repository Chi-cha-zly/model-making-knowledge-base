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
export default function RetrievalStatus({
  metadata,
}: {
  metadata: RetrievalMetadata
}) {
  return (
    <div className="evaluation-note">
      <p>
        实际策略：
        {metadata.effective_strategy === 'hybrid'
          ? '混合检索'
          : 'BM25 词法检索'}{' '}
        · {metadata.version}
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
      {metadata.query_truncated && <p>语义查询已按长度上限截取。</p>}
    </div>
  )
}
