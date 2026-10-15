export type Conversation = {
  id: number
  title: string
  created_at: string
  updated_at: string
}
export type Message = {
  id: number
  conversation_id: number
  role: 'user' | 'assistant'
  content: string
  created_at: string
}
export type Citation = {
  type: 'technique' | 'product'
  id: number
  title: string
  source_ids: number[]
  sources: { id: number; title: string; url: string | null }[]
  excerpt: string
  attachment_ids?: number[]
}
export type Run = {
  id: number
  conversation_id: number
  user_message_id: number
  assistant_message_id: number
  status: 'queued' | 'running' | 'completed' | 'stopped' | 'failed'
  content: string
  error: string | null
  citations: Citation[]
  created_at: string
  updated_at: string
}
export type ConversationDetail = Conversation & {
  messages: Message[]
  runs: Run[]
}
export type AgentConfig = {
  configured: boolean
  model: string | null
  mode?: 'rag' | 'agent'
  message: string
}
export type TraceStep =
  | { kind: 'search'; query: string; strategy: string; keys: string[] }
  | {
      kind: 'research'
      task: string
      searches: number
      covered: string[]
      missing: string[]
    }
  | { kind: 'read'; key: string; found: boolean }
  | { kind: 'invalid_citations'; keys: string[] }
export type AgentTrace = { run_id: number; steps: TraceStep[] }
export type AgentProgress = { activity: string; steps: TraceStep[] }
async function request<T>(
  path: string,
  method = 'GET',
  body?: unknown,
): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    method,
    ...(body === undefined
      ? {}
      : {
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        }),
  })
  if (!response.ok) {
    const data = await response.json().catch(() => null)
    throw new Error(
      typeof data?.detail === 'string'
        ? data.detail
        : `请求失败（${response.status}），请重试`,
    )
  }
  return response.status === 204 ? (undefined as T) : response.json()
}
export const chatApi = {
  config: () => request<AgentConfig>('/agent/config'),
  list: () => request<Conversation[]>('/conversations'),
  create: () => request<Conversation>('/conversations', 'POST', {}),
  detail: (id: number) => request<ConversationDetail>(`/conversations/${id}`),
  rename: (id: number, title: string) =>
    request<Conversation>(`/conversations/${id}`, 'PATCH', { title }),
  delete: (id: number) => request<void>(`/conversations/${id}`, 'DELETE'),
  start: (id: number, content: string, request_id: string) =>
    request<Run>(`/conversations/${id}/runs`, 'POST', { content, request_id }),
  stop: (id: number) => request<Run>(`/runs/${id}/stop`, 'POST'),
  retry: (id: number, request_id: string) =>
    request<Run>(`/runs/${id}/retry`, 'POST', { request_id }),
  steer: (id: number, content: string, request_id: string) =>
    request<Run>(`/runs/${id}/steer`, 'POST', { content, request_id }),
  run: (id: number) => request<Run>(`/runs/${id}`),
  trace: (id: number) => request<AgentTrace>(`/runs/${id}/trace`),
}
export const isActive = (run?: Run) =>
  run?.status === 'queued' || run?.status === 'running'
