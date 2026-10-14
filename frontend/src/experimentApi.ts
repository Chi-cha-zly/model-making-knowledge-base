export type IntegrationStatus = {
  provider: string
  model: string | null
  model_configured: boolean
  compare_models: string[]
  reasoning_effort: string
  max_output_tokens: number
  langsmith: {
    configured: boolean
    tracing_enabled: boolean
    project: string
    endpoint: string
    message: string
  }
}
export type ExperimentResult = {
  variant: string
  case_id: string
  query: string
  answer: string | null
  hit_keys: string[]
  metrics: Record<string, number | null>
  usage: {
    input_tokens: number
    output_tokens: number
    total_tokens: number
  } | null
  latency_ms: number
  cost_usd: number | null
  error: string | null
}
export type Experiment = {
  id: number
  dataset_id: string
  dataset_name: string
  corpus: 'demo' | 'live'
  mode: 'retrieval' | 'answers'
  models: string[]
  status: 'queued' | 'running' | 'completed' | 'failed' | 'stopped'
  progress: number
  total: number
  created_at: string
  updated_at: string
  error: string | null
  links: { label: string; url: string }[]
  results: ExperimentResult[]
}
export type ExperimentInput = {
  dataset_id: string
  mode: 'retrieval' | 'answers'
  models: string[]
  limit: number
  k: number
  request_id: string
}
async function request<T>(
  path: string,
  method = 'GET',
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    method,
    signal,
    ...(body
      ? {
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        }
      : {}),
  })
  const data = await response.json().catch(() => null)
  if (!response.ok)
    throw new Error(
      typeof data?.detail === 'string'
        ? data.detail
        : `实验请求失败（${response.status}），请稍后重试`,
    )
  return data as T
}
export const experimentApi = {
  status: (signal?: AbortSignal) =>
    request<IntegrationStatus>(
      '/integrations/status',
      'GET',
      undefined,
      signal,
    ),
  history: (signal?: AbortSignal) =>
    request<Experiment[]>('/experiments', 'GET', undefined, signal),
  get: (id: number, signal?: AbortSignal) =>
    request<Experiment>(`/experiments/${id}`, 'GET', undefined, signal),
  start: (body: ExperimentInput) =>
    request<Experiment>('/experiments', 'POST', body),
  stop: (id: number) => request<Experiment>(`/experiments/${id}/stop`, 'POST'),
}
