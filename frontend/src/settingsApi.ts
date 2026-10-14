export type ProfileInput = { name: string; base_url: string; model: string; compare_model: string; reasoning_effort: 'auto' | 'low' | 'medium' | 'high'; max_output_tokens: number; api_key?: string }
export type ProfileView = Omit<ProfileInput, 'api_key'> & { id: string; has_api_key: boolean; read_only: boolean }
export type SettingsView = { profiles: ProfileView[]; active_profile_id: string; custom_prompt: string; base_prompt: string; prompt_presets: { id: string; name: string; text: string }[]; storage_notice: string }
export type ConnectionTest = { ok: boolean; message: string; latency_ms: number | null }
async function request<T>(path = '', method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch(`/api/v1/settings${path}`, { method, ...(method !== 'GET' ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}) } : {}) })
  if (!response.ok) {
    // Never render upstream response bodies or validation inputs, which can contain secrets.
    const messages: Record<number, string> = { 409: '当前有任务运行或连接正在使用，请停止任务或切换连接后重试。', 422: '设置未通过校验，请检查接口地址、模型和字段范围。', 403: '请求来源未通过验证，请从本机应用重试。' }
    throw new Error(messages[response.status] ?? `设置操作失败（${response.status}），请稍后重试。`)
  }
  return response.json() as Promise<T>
}
export const settingsApi = {
  get: () => request<SettingsView>(),
  save: (body: ProfileInput, id?: string) => request<SettingsView>(id ? `/profiles/${encodeURIComponent(id)}` : '/profiles', id ? 'PUT' : 'POST', body),
  remove: (id: string) => request<SettingsView>(`/profiles/${encodeURIComponent(id)}`, 'DELETE'),
  activate: (id: string) => request<SettingsView>(`/profiles/${encodeURIComponent(id)}/activate`, 'POST'),
  prompt: (text: string) => request<SettingsView>('/prompt', 'PUT', { text }),
  test: (id: string) => request<ConnectionTest>(`/profiles/${encodeURIComponent(id)}/test`, 'POST'),
}
