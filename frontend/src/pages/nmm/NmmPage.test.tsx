import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import {
  nmmApi,
  NmmApiError,
  type NmmProject,
  type NmmEstimate,
} from '../../nmmApi'
import NmmPage from './NmmPage'
import { prepareOriginal } from './prepareOriginal'
vi.mock('./prepareOriginal', () => ({ prepareOriginal: vi.fn() }))

vi.mock('../../nmmApi', async (original) => ({
  ...(await original<typeof import('../../nmmApi')>()),
  nmmApi: {
    list: vi.fn(),
    detail: vi.fn(),
    create: vi.fn(),
    estimate: vi.fn(),
    run: vi.fn(),
    confirm: vi.fn(),
    stop: vi.fn(),
    remove: vi.fn(),
    downloadExport: vi.fn(),
  },
}))
vi.mock('./MaskEditor', () => ({
  default: ({
    onChange,
    file,
  }: {
    onChange: (mask: Blob) => void
    file: File
  }) => (
    <button
      type="button"
      data-file-name={file.name}
      onClick={() => onChange(new Blob(['mask'], { type: 'image/png' }))}
    >
      绘制测试遮罩
    </button>
  ),
}))
const params = {
  metal: 'gold',
  light_direction: 'top_left',
  contrast: 'medium',
  step_count: 7,
  notes: '',
} as const
function project(overrides: Partial<NmmProject> = {}): NmmProject {
  return {
    id: 'p1',
    title: '金色肩甲',
    status: 'draft',
    phase: 'draft',
    revision: 1,
    params,
    provider_mode: 'live',
    generation_count: 0,
    selected_candidate_id: null,
    active_job_id: null,
    thumbnail_asset_id: null,
    warnings: [],
    error: null,
    created_at: '',
    updated_at: '',
    assets: [],
    candidate_ids: [],
    plan: null,
    steps: [],
    meta: {
      usage: {
        calls: [],
        reserved_image_calls: 0,
        reserved_vision_calls: 0,
        known_costs: {},
        unknown_cost_calls: 0,
      },
      authorization: null,
      error_info: null,
    },
    ...overrides,
  }
}
const quote: NmmEstimate = {
  generation_required: true,
  revision: 1,
  generation_count: 0,
  generation_limit: 6,
  remaining_generations: 6,
  expected_image_calls: 1,
  expected_vision_calls: 5,
  provider_mode: 'live',
  estimated_cost: null,
  estimate_id: 'quote1',
  remaining_image_calls: 6,
  remaining_vision_calls: 30,
  current_config: [
    { provider: 'OpenAI', model: 'image-model' },
    { provider: 'Bailian', model: 'vision-model' },
  ],
}
class Events {
  static instances: Events[] = []
  listeners: Record<string, () => void> = {}
  onerror: (() => void) | null = null
  close = vi.fn()
  constructor() {
    Events.instances.push(this)
  }
  addEventListener(name: string, fn: () => void) {
    this.listeners[name] = fn
  }
}
function mount(path = '/nmm/p1') {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/nmm/*" element={<NmmPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return client
}
beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(prepareOriginal).mockImplementation(async (file) => ({
    file,
    sourceName: file.name,
    sourceWidth: 1600,
    sourceHeight: 1200,
    width: 1600,
    height: 1200,
    resized: false,
  }))
  Events.instances = []
  vi.stubGlobal('EventSource', Events)
  vi.mocked(nmmApi.detail).mockResolvedValue(project())
  vi.mocked(nmmApi.list).mockResolvedValue([project()])
  vi.mocked(nmmApi.estimate).mockResolvedValue(quote)
  vi.mocked(nmmApi.run).mockResolvedValue({ id: 9 } as never)
  vi.mocked(nmmApi.confirm).mockResolvedValue({ id: 9 } as never)
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})
async function approve() {
  await userEvent.click(
    await screen.findByRole('checkbox', { name: /接受总费用未知/ }),
  )
  await userEvent.click(screen.getByRole('button', { name: '批准并继续' }))
}
it('requires an explicit quote and unknown-cost approval before any generation', async () => {
  mount()
  await userEvent.click(
    await screen.findByRole('button', { name: '估算并生成候选' }),
  )
  expect(
    await screen.findByRole('dialog', { name: '确认本次调用' }),
  ).toBeInTheDocument()
  expect(screen.getByText('image-model')).toBeInTheDocument()
  expect(nmmApi.run).not.toHaveBeenCalled()
  expect(screen.getByRole('button', { name: '批准并继续' })).toBeDisabled()
  await approve()
  await waitFor(() => expect(nmmApi.run).toHaveBeenCalledTimes(1))
  expect(vi.mocked(nmmApi.run).mock.calls[0][1]).toMatchObject({
    revision: 1,
    confirm_generation: true,
    cost_authorization: {
      estimate_id: 'quote1',
      accept_unknown_cost: true,
      max_image_calls: 1,
      max_vision_calls: 5,
    },
  })
})
it('retries a network-uncertain operation only on click with the identical request body', async () => {
  vi.mocked(nmmApi.run).mockRejectedValueOnce(new NmmApiError(0, '网络中断'))
  mount()
  await userEvent.click(
    await screen.findByRole('button', { name: '估算并生成候选' }),
  )
  await approve()
  const retry = await screen.findByRole('button', { name: '重试原操作' })
  expect(nmmApi.run).toHaveBeenCalledTimes(1)
  const original = vi.mocked(nmmApi.run).mock.calls[0][1]
  await userEvent.click(retry)
  await waitFor(() => expect(nmmApi.run).toHaveBeenCalledTimes(2))
  expect(vi.mocked(nmmApi.run).mock.calls[1][1]).toEqual(original)
  expect(nmmApi.estimate).toHaveBeenCalledTimes(1)
})
it('refreshes a 409 without retrying or silently accepting a new quote', async () => {
  vi.mocked(nmmApi.run).mockRejectedValueOnce(
    new NmmApiError(409, '估算已失效'),
  )
  mount()
  await userEvent.click(
    await screen.findByRole('button', { name: '估算并生成候选' }),
  )
  await approve()
  expect(await screen.findByText(/请重新估算/)).toBeInTheDocument()
  expect(nmmApi.run).toHaveBeenCalledTimes(1)
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(
    screen.queryByRole('button', { name: '重试原操作' }),
  ).not.toBeInTheDocument()
  await waitFor(() => expect(nmmApi.detail).toHaveBeenCalledTimes(2))
})
it('binds changed candidate parameters to a fresh proposal and regenerate operation', async () => {
  vi.mocked(nmmApi.detail).mockResolvedValue(
    project({ status: 'awaiting_confirmation', candidate_ids: ['c1'] }),
  )
  mount()
  await userEvent.selectOptions(
    await screen.findByLabelText('金属效果'),
    'silver',
  )
  await userEvent.click(
    screen.getByRole('button', { name: '修改参数并重新估算' }),
  )
  await waitFor(() =>
    expect(nmmApi.estimate).toHaveBeenCalledWith('p1', {
      ...params,
      metal: 'silver',
    }),
  )
  await approve()
  await waitFor(() =>
    expect(nmmApi.confirm).toHaveBeenCalledWith(
      'p1',
      expect.objectContaining({
        action: 'regenerate',
        params: { ...params, metal: 'silver' },
        confirm_generation: true,
      }),
    ),
  )
})
it('requires confirmation for choosing a candidate, with no automatic selection', async () => {
  vi.mocked(nmmApi.detail).mockResolvedValue(
    project({ status: 'awaiting_confirmation', candidate_ids: ['c1'] }),
  )
  mount()
  await userEvent.click(
    await screen.findByRole('button', { name: '选择候选 1' }),
  )
  expect(nmmApi.confirm).not.toHaveBeenCalled()
  expect(
    await screen.findByText(/后续教学分析仍可能产生视觉调用费用/),
  ).toBeInTheDocument()
  await approve()
  await waitFor(() =>
    expect(nmmApi.confirm).toHaveBeenCalledWith(
      'p1',
      expect.objectContaining({
        action: 'select',
        candidate_id: 'c1',
        confirm_generation: false,
      }),
    ),
  )
})
it('closes disconnected events, offers read-only refresh, and stops without generating', async () => {
  vi.mocked(nmmApi.detail).mockResolvedValue(
    project({ status: 'generating', active_job_id: 9 }),
  )
  vi.mocked(nmmApi.stop).mockResolvedValue(project({ status: 'stopped' }))
  mount()
  await screen.findByRole('button', { name: '停止任务' })
  await waitFor(() => expect(Events.instances).toHaveLength(1))
  fireEvent(window, new Event('online'))
  Events.instances[0].onerror?.()
  expect(await screen.findByText(/连接中断/)).toBeInTheDocument()
  expect(Events.instances[0].close).toHaveBeenCalled()
  await userEvent.click(screen.getByRole('button', { name: '停止任务' }))
  await waitFor(() => expect(nmmApi.stop).toHaveBeenCalledWith('p1'))
  expect(nmmApi.run).not.toHaveBeenCalled()
  expect(nmmApi.confirm).not.toHaveBeenCalled()
})
it('requires a drawn mask and submits a local project without generation', async () => {
  vi.mocked(nmmApi.create).mockResolvedValue(project())
  mount('/nmm/new')
  const submit = screen.getByRole('button', { name: '保存本地项目' })
  expect(submit).toBeDisabled()
  await userEvent.type(screen.getByLabelText('项目名称'), '肩甲练习')
  await userEvent.upload(
    screen.getByLabelText('原图'),
    new File(['image'], 'armor.png', { type: 'image/png' }),
  )
  expect(submit).toBeDisabled()
  await userEvent.click(screen.getByRole('button', { name: '绘制测试遮罩' }))
  await userEvent.click(submit)
  await waitFor(() =>
    expect(nmmApi.create).toHaveBeenCalledWith(
      expect.objectContaining({
        title: '肩甲练习',
        mask: expect.any(Blob),
        references: [],
      }),
    ),
  )
  expect(nmmApi.run).not.toHaveBeenCalled()
})
it('requires a second confirmation before deleting a project', async () => {
  mount('/nmm')
  await userEvent.click(
    await screen.findByRole('button', { name: '删除 金色肩甲' }),
  )
  expect(nmmApi.remove).not.toHaveBeenCalled()
  const dialog = screen.getByRole('dialog', { name: '删除项目' })
  await userEvent.click(
    within(dialog).getByRole('button', { name: '确认删除' }),
  )
  await waitFor(() => expect(nmmApi.remove).toHaveBeenCalledWith('p1'))
})
it('switches result steps, exposes a difference overlay and current-output downloads', async () => {
  vi.mocked(nmmApi.detail).mockResolvedValue(
    project({
      status: 'done',
      assets: [
        { id: 'original', role: 'original' },
        { id: 'final', role: 'final' },
      ] as NmmProject['assets'],
      steps: [1, 2].map((index) => ({
        id: `s${index}`,
        plan_id: 'plan',
        index,
        title: `步骤 ${index}`,
        image_asset_id: `image${index}`,
        diff_asset_id: `diff${index}`,
        description: {
          index,
          color_hex: '#c0a060',
          area_ratio: 0.2,
          technique: `技法 ${index}`,
        },
        warnings: [],
      })),
    }),
  )
  mount()
  await userEvent.click(
    await screen.findByRole('button', { name: '查看步骤 2' }),
  )
  expect(screen.getByText('技法 2')).toBeInTheDocument()
  expect(screen.getByText(/请求 7 步 · 实际 2 步/)).toBeInTheDocument()
  await userEvent.click(screen.getByLabelText('显示本步变化'))
  expect(screen.getByRole('img', { name: '本步变化区域' })).toHaveStyle({
    maskImage: 'url("/api/v1/nmm/assets/diff2")',
  })
  expect(screen.getByRole('link', { name: '下载本步图片' })).toHaveAttribute(
    'href',
    '/api/v1/nmm/assets/image2?download=1',
  )
  expect(screen.getByRole('button', { name: '下载成果 ZIP' })).toBeEnabled()
})

it('ignores an estimate returned after the project revision changes', async () => {
  let resolve!: (value: NmmEstimate) => void
  vi.mocked(nmmApi.estimate).mockReturnValue(
    new Promise((done) => {
      resolve = done
    }),
  )
  const client = mount()
  await userEvent.click(
    await screen.findByRole('button', { name: '估算并生成候选' }),
  )
  client.setQueryData(['nmm', 'project', 'p1'], project({ revision: 2 }))
  await screen.findByText(/版本 2/)
  resolve(quote)
  await waitFor(() =>
    expect(
      screen.getByRole('button', { name: '估算并生成候选' }),
    ).toBeEnabled(),
  )
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(nmmApi.run).not.toHaveBeenCalled()
})
it('keeps selection available at the image cap and defaults to zero new images', async () => {
  vi.mocked(nmmApi.detail).mockResolvedValue(
    project({
      status: 'awaiting_confirmation',
      generation_count: 6,
      candidate_ids: ['c1'],
    }),
  )
  vi.mocked(nmmApi.estimate).mockResolvedValue({
    ...quote,
    remaining_generations: 0,
    remaining_image_calls: 0,
    remaining_vision_calls: 2,
  })
  mount()
  await userEvent.click(
    await screen.findByRole('button', { name: '选择候选 1' }),
  )
  expect(await screen.findByLabelText('本次图片调用上限')).toHaveValue(0)
  expect(screen.getByLabelText('本次视觉调用上限')).toHaveValue(2)
  await approve()
  await waitFor(() =>
    expect(nmmApi.confirm).toHaveBeenCalledWith(
      'p1',
      expect.objectContaining({
        cost_authorization: expect.objectContaining({
          max_image_calls: 0,
          max_vision_calls: 2,
        }),
      }),
    ),
  )
})
it('can enlarge each candidate and return keyboard focus without selecting it', async () => {
  vi.mocked(nmmApi.detail).mockResolvedValue(
    project({ status: 'awaiting_confirmation', candidate_ids: ['c1', 'c2'] }),
  )
  mount()
  const button = await screen.findByRole('button', { name: '放大候选 2' })
  await userEvent.click(button)
  expect(
    screen.getByRole('dialog', { name: '候选效果预览' }),
  ).toBeInTheDocument()
  expect(screen.getByAltText('候选效果放大预览')).toHaveAttribute(
    'src',
    '/api/v1/nmm/assets/c2',
  )
  await userEvent.keyboard('{Escape}')
  expect(button).toHaveFocus()
  expect(nmmApi.confirm).not.toHaveBeenCalled()
})
it('keeps the same operation after a server error and closes events on terminal state', async () => {
  vi.mocked(nmmApi.run).mockRejectedValueOnce(
    new NmmApiError(500, '服务暂不可用'),
  )
  const client = mount()
  await userEvent.click(
    await screen.findByRole('button', { name: '估算并生成候选' }),
  )
  await approve()
  await userEvent.click(
    await screen.findByRole('button', { name: '重试原操作' }),
  )
  expect(vi.mocked(nmmApi.run).mock.calls[1][1]).toEqual(
    vi.mocked(nmmApi.run).mock.calls[0][1],
  )
  client.setQueryData(
    ['nmm', 'project', 'p1'],
    project({ status: 'generating' }),
  )
  await waitFor(() => expect(Events.instances).toHaveLength(1))
  client.setQueryData(
    ['nmm', 'project', 'p1'],
    project({ status: 'awaiting_confirmation' }),
  )
  await waitFor(() => expect(Events.instances[0].close).toHaveBeenCalled())
})
it('blocks unsupported original files and excessive reference counts before saving', async () => {
  mount('/nmm/new')
  fireEvent.change(screen.getByLabelText('原图'), {
    target: {
      files: [new File(['bad'], 'vector.svg', { type: 'image/svg+xml' })],
    },
  })
  expect(screen.getByRole('alert')).toHaveTextContent('仅支持 PNG')
  fireEvent.change(screen.getByLabelText('添加参考'), {
    target: {
      files: Array.from(
        { length: 13 },
        (_, i) => new File(['img'], `${i}.png`, { type: 'image/png' }),
      ),
    },
  })
  expect(await screen.findByText('参考图片最多 12 张。')).toHaveAttribute(
    'role',
    'alert',
  )
  expect(nmmApi.create).not.toHaveBeenCalled()
})
it('allows browsing unconfigured projects while preventing generation', async () => {
  vi.mocked(nmmApi.detail).mockResolvedValue(
    project({ provider_mode: 'unconfigured' }),
  )
  mount()
  expect(
    await screen.findByRole('button', { name: '估算并生成候选' }),
  ).toBeDisabled()
  expect(screen.getByText(/模型尚未配置。项目已保存在本地/)).toBeInTheDocument()
  expect(nmmApi.estimate).not.toHaveBeenCalled()
})

it('keeps ZIP failures on the page instead of navigating to an error JSON', async () => {
  vi.mocked(nmmApi.detail).mockResolvedValue(
    project({
      status: 'done',
      assets: [{ id: 'final', role: 'final' }] as NmmProject['assets'],
    }),
  )
  vi.mocked(nmmApi.downloadExport).mockRejectedValue(
    new NmmApiError(413, '成果包超过下载上限'),
  )
  mount()
  await userEvent.click(
    await screen.findByRole('button', { name: '下载成果 ZIP' }),
  )
  expect(await screen.findByRole('alert')).toHaveTextContent(
    '成果包超过下载上限',
  )
  expect(nmmApi.downloadExport).toHaveBeenCalledTimes(1)
})

it('prevents approval above the remaining vision budget or without consent', async () => {
  vi.mocked(nmmApi.estimate).mockResolvedValue({
    ...quote,
    remaining_vision_calls: 2,
  })
  mount()
  await userEvent.click(
    await screen.findByRole('button', { name: '估算并生成候选' }),
  )
  await screen.findByRole('dialog')
  fireEvent.change(screen.getByLabelText('本次视觉调用上限'), {
    target: { value: '3' },
  })
  await userEvent.click(
    screen.getByRole('checkbox', { name: /接受总费用未知/ }),
  )
  expect(screen.getByRole('button', { name: '批准并继续' })).toBeDisabled()
  expect(nmmApi.run).not.toHaveBeenCalled()
  await userEvent.keyboard('{Escape}')
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
})
it('closes the EventSource when navigating away and never restarts a saved active job', async () => {
  vi.mocked(nmmApi.detail).mockResolvedValue(
    project({ status: 'planning', active_job_id: 9 }),
  )
  mount()
  await waitFor(() => expect(Events.instances).toHaveLength(1))
  await userEvent.click(screen.getByRole('link', { name: '← 项目列表' }))
  await screen.findByRole('heading', { name: 'NMM 涂装工作台' })
  expect(Events.instances[0].close).toHaveBeenCalled()
  expect(nmmApi.run).not.toHaveBeenCalled()
  expect(nmmApi.confirm).not.toHaveBeenCalled()
})
it('downloads a successful ZIP through a temporary URL and releases it on unmount', async () => {
  const revoke = vi.fn()
  vi.stubGlobal(
    'URL',
    class extends URL {
      static createObjectURL = vi.fn(() => 'blob:test-output')
      static revokeObjectURL = revoke
    },
  )
  const click = vi
    .spyOn(HTMLAnchorElement.prototype, 'click')
    .mockImplementation(() => {})
  vi.mocked(nmmApi.detail).mockResolvedValue(
    project({
      status: 'done',
      assets: [{ id: 'final', role: 'final' }] as NmmProject['assets'],
    }),
  )
  vi.mocked(nmmApi.downloadExport).mockResolvedValue(
    new Blob(['zip'], { type: 'application/zip' }),
  )
  mount()
  await userEvent.click(
    await screen.findByRole('button', { name: '下载成果 ZIP' }),
  )
  await waitFor(() => expect(click).toHaveBeenCalledTimes(1))
  expect(document.querySelector('a[download]')).not.toBeInTheDocument()
  cleanup()
  expect(revoke).toHaveBeenCalledWith('blob:test-output')
  click.mockRestore()
})

it('limits style/palette references together and releases reference preview URLs', async () => {
  const revoke = vi.fn()
  let count = 0
  vi.stubGlobal(
    'URL',
    class extends URL {
      static createObjectURL = vi.fn(() => `blob:ref-${++count}`)
      static revokeObjectURL = revoke
    },
  )
  const close = vi.fn()
  vi.stubGlobal(
    'createImageBitmap',
    vi.fn().mockResolvedValue({ width: 16, height: 16, close }),
  )
  mount('/nmm/new')
  await userEvent.upload(
    screen.getByLabelText('添加参考'),
    Array.from(
      { length: 4 },
      (_, i) => new File(['img'], `${i}.png`, { type: 'image/png' }),
    ),
  )
  await screen.findByLabelText('参考 4 的用途')
  for (let i = 1; i <= 3; i++)
    await userEvent.selectOptions(
      screen.getByLabelText(`参考 ${i} 的用途`),
      'reference_style',
    )
  await userEvent.selectOptions(
    screen.getByLabelText('参考 4 的用途'),
    'reference_palette',
  )
  expect(screen.getByRole('alert')).toHaveTextContent(
    '效果参考与配色参考合计最多 3 张',
  )
  expect(screen.getByLabelText('参考 4 的用途')).toHaveValue('reference_steps')
  expect(close).toHaveBeenCalledTimes(4)
  cleanup()
  expect(revoke).toHaveBeenCalledTimes(4)
})
it('rejects references above the pixel limit and closes their bitmap', async () => {
  const close = vi.fn()
  vi.stubGlobal(
    'createImageBitmap',
    vi.fn().mockResolvedValue({ width: 5000, height: 5000, close }),
  )
  mount('/nmm/new')
  await userEvent.upload(
    screen.getByLabelText('添加参考'),
    new File(['img'], 'huge.png', { type: 'image/png' }),
  )
  expect(await screen.findByRole('alert')).toHaveTextContent(
    '参考图片不能超过 2000 万像素',
  )
  expect(close).toHaveBeenCalledTimes(1)
  expect(screen.queryByLabelText('参考 1 的用途')).not.toBeInTheDocument()
})

it('resumes a backend-confirmed cached generation at the image cap with zero new images', async () => {
  vi.mocked(nmmApi.detail).mockResolvedValue(
    project({
      status: 'stopped',
      generation_count: 6,
      candidate_ids: ['cached'],
      selected_candidate_id: null,
    }),
  )
  vi.mocked(nmmApi.estimate).mockResolvedValue({
    ...quote,
    generation_required: false,
    expected_image_calls: 0,
    expected_vision_calls: 0,
    remaining_generations: 0,
    remaining_image_calls: 0,
  })
  mount()
  await userEvent.click(
    await screen.findByRole('button', { name: '估算并继续任务' }),
  )
  expect(await screen.findByLabelText('本次图片调用上限')).toHaveValue(0)
  expect(screen.getByLabelText('本次视觉调用上限')).toHaveValue(1)
  expect(
    screen.getByText(/恢复已保存结果，当前预计无需新的模型调用/),
  ).toBeInTheDocument()
  await approve()
  await waitFor(() =>
    expect(nmmApi.run).toHaveBeenCalledWith(
      'p1',
      expect.objectContaining({
        confirm_generation: false,
        cost_authorization: expect.objectContaining({
          max_image_calls: 0,
          max_vision_calls: 1,
        }),
      }),
    ),
  )
})
it('explains the positive-authorization limit if cached recovery has no vision allowance', async () => {
  vi.mocked(nmmApi.detail).mockResolvedValue(
    project({ status: 'stopped', generation_count: 6 }),
  )
  vi.mocked(nmmApi.estimate).mockResolvedValue({
    ...quote,
    generation_required: false,
    expected_image_calls: 0,
    expected_vision_calls: 0,
    remaining_generations: 0,
    remaining_image_calls: 0,
    remaining_vision_calls: 0,
  })
  mount()
  await userEvent.click(
    await screen.findByRole('button', { name: '估算并继续任务' }),
  )
  expect(await screen.findByText(/视觉额度已耗尽/)).toBeInTheDocument()
  await userEvent.click(
    screen.getByRole('checkbox', { name: /接受总费用未知/ }),
  )
  expect(screen.getByRole('button', { name: '批准并继续' })).toBeDisabled()
  expect(nmmApi.run).not.toHaveBeenCalled()
})

it('shows the source and working sizes and saves the exact prepared file used by the mask editor', async () => {
  const original = new File(['source'], 'large.jpg', { type: 'image/jpeg' })
  const working = new File(['working'], 'large-work.png', { type: 'image/png' })
  vi.mocked(prepareOriginal).mockResolvedValue({
    file: working,
    sourceName: original.name,
    sourceWidth: 4000,
    sourceHeight: 3000,
    width: 2309,
    height: 1732,
    resized: true,
  })
  vi.mocked(nmmApi.create).mockResolvedValue(project())
  mount('/nmm/new')
  await userEvent.upload(screen.getByLabelText('原图'), original)
  expect(await screen.findByText(/已选择：large.jpg/)).toBeInTheDocument()
  expect(screen.getByText(/4000 × 3000.*2309 × 1732/)).toBeInTheDocument()
  expect(screen.getByText(/仅在浏览器本地缩小/)).toBeInTheDocument()
  const editor = screen.getByRole('button', { name: '绘制测试遮罩' })
  expect(editor).toHaveAttribute('data-file-name', working.name)
  await userEvent.type(screen.getByLabelText('项目名称'), '练习')
  await userEvent.click(editor)
  await userEvent.click(screen.getByRole('button', { name: '保存本地项目' }))
  await waitFor(() =>
    expect(nmmApi.create).toHaveBeenCalledWith(
      expect.objectContaining({ original: working }),
    ),
  )
  expect(nmmApi.run).not.toHaveBeenCalled()
})
it('clears the previous mask while preparing and ignores a late result after switching files', async () => {
  let first!: (value: Awaited<ReturnType<typeof prepareOriginal>>) => void
  let second!: (value: Awaited<ReturnType<typeof prepareOriginal>>) => void
  vi.mocked(prepareOriginal)
    .mockReturnValueOnce(
      new Promise((done) => {
        first = done
      }),
    )
    .mockReturnValueOnce(
      new Promise((done) => {
        second = done
      }),
    )
  mount('/nmm/new')
  await userEvent.type(screen.getByLabelText('项目名称'), '练习')
  await userEvent.upload(
    screen.getByLabelText('原图'),
    new File(['a'], 'a.png', { type: 'image/png' }),
  )
  expect(await screen.findByText(/正在本地处理原图/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '保存本地项目' })).toBeDisabled()
  await userEvent.upload(
    screen.getByLabelText('原图'),
    new File(['b'], 'b.png', { type: 'image/png' }),
  )
  expect(vi.mocked(prepareOriginal).mock.calls[0][1].aborted).toBe(true)
  const b = new File(['b-work'], 'b-work.png', { type: 'image/png' })
  second({
    file: b,
    sourceName: 'b.png',
    sourceWidth: 4000,
    sourceHeight: 3000,
    width: 2309,
    height: 1732,
    resized: true,
  })
  const editor = await screen.findByRole('button', { name: '绘制测试遮罩' })
  await userEvent.click(editor)
  first({
    file: new File(['a'], 'a.png', { type: 'image/png' }),
    sourceName: 'a.png',
    sourceWidth: 10,
    sourceHeight: 10,
    width: 10,
    height: 10,
    resized: false,
  })
  await waitFor(() =>
    expect(editor).toHaveAttribute('data-file-name', 'b-work.png'),
  )
  expect(screen.getByText(/已选择：b.png/)).toBeInTheDocument()
  expect(nmmApi.create).not.toHaveBeenCalled()
})
it('explains incomplete local preparation and aborts it when the page unmounts', async () => {
  vi.mocked(prepareOriginal).mockReturnValue(new Promise(() => {}))
  mount('/nmm/new')
  expect(screen.getByText(/请先选择原图/)).toBeInTheDocument()
  await userEvent.upload(
    screen.getByLabelText('原图'),
    new File(['a'], 'a.png', { type: 'image/png' }),
  )
  expect(screen.getByRole('button', { name: '保存本地项目' })).toBeDisabled()
  cleanup()
  expect(vi.mocked(prepareOriginal).mock.calls[0][1].aborted).toBe(true)
})
it('shows a missing NMM endpoint with HTTP status and older-backend guidance', async () => {
  vi.mocked(nmmApi.list).mockRejectedValue(new NmmApiError(404, 'Not Found'))
  mount('/nmm')
  const alert = await screen.findByRole('alert')
  expect(alert).toHaveTextContent('HTTP 404')
  expect(alert).toHaveTextContent('Not Found')
  expect(alert).toHaveTextContent(/旧版后端|尚未启用/)
})
