/** HTTP-only NMM protocol. Mutations never retry automatically. */
const BASE = '/api/v1/nmm'

export type NmmParams = {
  metal: string
  light_direction:
    | 'top'
    | 'top_left'
    | 'top_right'
    | 'left'
    | 'right'
    | 'front'
    | 'bottom'
    | 'bottom_left'
    | 'bottom_right'
    | 'back'
  contrast: 'low' | 'medium' | 'high'
  step_count: number
  notes: string
}
export type ReferenceRole =
  'reference_style' | 'reference_palette' | 'reference_steps'
export type NmmAsset = {
  id: string
  project_id: string
  revision: number
  role:
    | ReferenceRole
    | 'original'
    | 'mask'
    | 'candidate'
    | 'final'
    | 'step'
    | 'step_diff'
    | 'contact_sheet'
  content_type: string
  size: number
  width: number
  height: number
  sha256: string
  sort_order: number
  meta: {
    index?: number
    kind?: string
    palette_index?: number | null
    area_ratio?: number
    attempt?: number
  }
  created_at: string
}
export type NmmStatus =
  | 'draft'
  | 'generating'
  | 'awaiting_confirmation'
  | 'planning'
  | 'rendering'
  | 'done'
  | 'failed'
  | 'stopped'
export type ProviderMode = 'unconfigured' | 'offline_test' | 'live'
export type NmmCallUsage = {
  provider: string
  model: string
  input_tokens: number | null
  output_tokens: number | null
  total_tokens: number | null
  input_images: number
  output_images: number
  elapsed_ms: number | null
  estimated_cost: number | null
  currency: string | null
  request_id: string | null
}
export type NmmErrorInfo = { code: string; message: string; retryable: boolean }
export type NmmPublicMeta = {
  usage: {
    calls: {
      id: string
      kind: 'image' | 'vision'
      revision: number
      status: 'requested' | 'completed' | 'failed' | 'unknown'
      created_at: string
      usage: NmmCallUsage | null
      error: NmmErrorInfo | null
    }[]
    reserved_image_calls: number
    reserved_vision_calls: number
    known_costs: Record<string, number>
    unknown_cost_calls: number
  }
  authorization: {
    remaining_image_calls: number
    remaining_vision_calls: number
    disabled: boolean
  } | null
  error_info: NmmErrorInfo | null
}
export type NmmProjectSummary = {
  id: string
  title: string
  status: NmmStatus
  phase: string
  params: NmmParams
  revision: number
  selected_candidate_id: string | null
  active_job_id: number | null
  generation_count: number
  warnings: string[]
  error: string | null
  created_at: string
  updated_at: string
  provider_mode: ProviderMode
  meta: NmmPublicMeta
  thumbnail_asset_id: string | null
}
export type NmmInstruction = {
  index: number
  color_hex: string | string[] | null
  area_ratio: number
  title?: string
  color_description?: string
  areas?: string
  technique?: string
  expected_result?: string
  change_from_previous?: string
  common_mistakes?: string
  needs_confirmation?: string[]
}
export type NmmStep = {
  id: string
  plan_id: string
  index: number
  title: string
  image_asset_id: string
  diff_asset_id: string | null
  description: NmmInstruction
  warnings: string[]
}
export type NmmPaletteColor = {
  hex: string
  lab: [number, number, number]
  area_ratio: number
}
export type NmmPlan = {
  id: string
  project_id: string
  revision: number
  candidate_id: string
  plan: { steps: NmmInstruction[]; overall_tips: string[]; fallback?: boolean }
  palette: NmmPaletteColor[]
  renderer_version: string
  render_params: Record<string, number>
  created_at: string
}
export type NmmProject = NmmProjectSummary & {
  assets: NmmAsset[]
  candidate_ids: string[]
  plan: NmmPlan | null
  steps: NmmStep[]
  analysis?: Record<string, unknown> | null
  workflow_thread_id?: string | null
}
export type NmmEstimate = {
  generation_required: boolean
  revision: number
  generation_count: number
  generation_limit: number
  remaining_generations: number
  expected_image_calls: number
  expected_vision_calls: number
  provider_mode: ProviderMode
  estimated_cost: number | null
  estimate_id?: string
  target_revision?: number
  current_config?: {
    provider: string
    model?: string | null
    base_url?: string
    quality?: string
    region?: string
  }[]
  requires_authorization?: boolean
  remaining_image_calls?: number
  remaining_vision_calls?: number
  cost_notice?: string
}
export type CostAuthorization = {
  estimate_id: string
  max_image_calls: number
  max_vision_calls: number
  accept_unknown_cost: boolean
}
export type NmmRunInput = {
  request_id: string
  revision: number
  confirm_generation: boolean
  cost_authorization?: CostAuthorization | null
}
export type NmmConfirmInput = {
  request_id: string
  revision: number
  action: 'select' | 'regenerate'
  candidate_id?: string
  params?: NmmParams
  confirm_generation?: boolean
  cost_authorization?: CostAuthorization | null
}
export type NmmJob = {
  id: number
  kind: string
  request_id: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'stopped'
  progress: number
  total: number
  result: Record<string, unknown> | null
  error: string | null
  created_at: string
  updated_at: string
}

export class NmmApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message)
    this.name = 'NmmApiError'
  }
}

const projectPath = (id: string) => `${BASE}/projects/${encodeURIComponent(id)}`
export const assetUrl = (id: string) =>
  `${BASE}/assets/${encodeURIComponent(id)}`
export const exportUrl = (id: string) => `${projectPath(id)}/export`
export const eventsUrl = (id: string) => `${projectPath(id)}/events`

async function request<T>(
  url: string,
  init?: RequestInit,
  binary = false,
): Promise<T> {
  let response: Response
  try {
    response = await fetch(url, init)
  } catch {
    throw new NmmApiError(0, '网络请求未完成，请检查连接后使用相同操作重试。')
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    throw new NmmApiError(
      response.status,
      typeof body?.detail === 'string'
        ? body.detail
        : `请求失败（${response.status}）`,
    )
  }
  if (response.status === 204) return undefined as T
  try {
    return (await (binary ? response.blob() : response.json())) as T
  } catch {
    throw new NmmApiError(0, '响应未能完整读取，请使用相同操作重试以核对结果。')
  }
}
const json = (body: unknown): RequestInit => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

export const nmmApi = {
  downloadExport: (id: string) => request<Blob>(exportUrl(id), undefined, true),
  list: () => request<NmmProjectSummary[]>(`${BASE}/projects`),
  detail: (id: string) => request<NmmProject>(projectPath(id)),
  create: (input: {
    title: string
    params: NmmParams
    original: File
    mask: Blob
    references: { file: File; role: ReferenceRole }[]
  }) => {
    const body = new FormData()
    body.append('title', input.title)
    body.append('params', JSON.stringify(input.params))
    body.append('original', input.original)
    body.append('mask', input.mask, 'mask.png')
    for (const reference of input.references) {
      body.append('references', reference.file)
      body.append('roles', reference.role)
    }
    return request<NmmProject>(`${BASE}/projects`, { method: 'POST', body })
  },
  estimate: (id: string, params?: NmmParams) =>
    request<NmmEstimate>(
      `${projectPath(id)}/estimate`,
      params === undefined ? undefined : json({ params }),
    ),
  run: (id: string, body: NmmRunInput) =>
    request<NmmJob>(`${projectPath(id)}/run`, json(body)),
  confirm: (id: string, body: NmmConfirmInput) =>
    request<NmmJob>(`${projectPath(id)}/confirm`, json(body)),
  stop: (id: string) =>
    request<NmmProject>(`${projectPath(id)}/stop`, { method: 'POST' }),
  remove: (id: string) => request<void>(projectPath(id), { method: 'DELETE' }),
}
