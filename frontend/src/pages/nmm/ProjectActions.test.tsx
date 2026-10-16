import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { nmmApi, NmmApiError, type NmmProject } from '../../nmmApi'
import ProjectActions from './ProjectActions'

vi.mock('../../nmmApi', async (original) => ({
  ...(await original<typeof import('../../nmmApi')>()),
  nmmApi: { estimate: vi.fn(), confirm: vi.fn(), run: vi.fn() },
}))
const acknowledgement = '我已对照原图，确认零件、轮廓和浮雕没有被改动'
const check = {
  status: 'needs_review' as const,
  reasons: [] as string[],
  metrics: {},
  version: 'v1',
}
function project(overrides: Partial<NmmProject> = {}): NmmProject {
  return {
    id: 'p1',
    title: '肩甲',
    status: 'awaiting_confirmation',
    phase: 'review',
    revision: 1,
    params: {
      metal: 'gold',
      light_direction: 'top_left',
      contrast: 'medium',
      step_count: 7,
      notes: '',
    },
    provider_mode: 'offline_test',
    generation_count: 1,
    selected_candidate_id: null,
    active_job_id: null,
    thumbnail_asset_id: null,
    warnings: [],
    error: null,
    created_at: '',
    updated_at: '',
    assets: [
      { id: 'original', role: 'original', width: 1200, height: 800 },
    ] as NmmProject['assets'],
    candidate_ids: ['c1'],
    candidate_checks: { c1: check },
    plan: null,
    steps: [],
    meta: {
      usage: {
        calls: [],
        reserved_image_calls: 1,
        reserved_vision_calls: 1,
        known_costs: {},
        unknown_cost_calls: 0,
      },
      authorization: null,
      error_info: null,
    },
    ...overrides,
  }
}
beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(nmmApi.estimate).mockResolvedValue({
    generation_required: false,
    revision: 1,
    generation_count: 1,
    generation_limit: 6,
    remaining_generations: 5,
    expected_image_calls: 0,
    expected_vision_calls: 4,
    provider_mode: 'offline_test',
    estimated_cost: null,
  })
  vi.mocked(nmmApi.confirm).mockResolvedValue({ id: 9 } as never)
  vi.mocked(nmmApi.run).mockResolvedValue({ id: 9 } as never)
})
afterEach(cleanup)

it.each([
  ['missing', undefined, /尚未完成结构检查/],
  [
    'blocked',
    {
      c1: {
        ...check,
        status: 'blocked' as const,
        reasons: ['检测到轮廓明显偏移，请重新生成。'],
      },
    },
    /检测到轮廓明显偏移/,
  ],
])(
  'never estimates or selects a %s candidate',
  async (_, candidate_checks, warning) => {
    render(
      <ProjectActions
        project={project({ candidate_checks })}
        onRefresh={vi.fn()}
      />,
    )
    expect(screen.getByText(warning)).toBeInTheDocument()
    const choose = screen.getByRole('button', { name: '选择候选 1' })
    expect(choose).toBeDisabled()
    await userEvent.click(choose)
    expect(
      screen.queryByRole('checkbox', { name: acknowledgement }),
    ).not.toBeInTheDocument()
    expect(nmmApi.estimate).not.toHaveBeenCalled()
    expect(nmmApi.confirm).not.toHaveBeenCalled()
  },
)
it('requires fresh human review and preserves acknowledged selection in an uncertain retry', async () => {
  vi.mocked(nmmApi.confirm).mockRejectedValueOnce(new NmmApiError(0, '中断'))
  render(<ProjectActions project={project()} onRefresh={vi.fn()} />)
  const choose = screen.getByRole('button', { name: '选择候选 1' })
  expect(choose).toBeDisabled()
  await userEvent.click(screen.getByRole('checkbox', { name: acknowledgement }))
  await userEvent.click(choose)
  await userEvent.click(
    await screen.findByRole('button', { name: '批准并继续' }),
  )
  const retry = await screen.findByRole('button', { name: '重试原操作' })
  const body = vi.mocked(nmmApi.confirm).mock.calls[0][1]
  expect(body).toMatchObject({
    action: 'select',
    candidate_id: 'c1',
    quality_acknowledged: true,
    confirm_generation: false,
  })
  await userEvent.click(retry)
  await waitFor(() => expect(nmmApi.confirm).toHaveBeenCalledTimes(2))
  expect(vi.mocked(nmmApi.confirm).mock.calls[1][1]).toEqual(body)
  expect(nmmApi.estimate).toHaveBeenCalledTimes(1)
})
it('clears human review on revision, candidate replacement, and parameter edits', async () => {
  const refresh = vi.fn()
  const { rerender } = render(
    <ProjectActions project={project()} onRefresh={refresh} />,
  )
  await userEvent.click(screen.getByRole('checkbox', { name: acknowledgement }))
  rerender(
    <ProjectActions project={project({ revision: 2 })} onRefresh={refresh} />,
  )
  expect(
    screen.getByRole('checkbox', { name: acknowledgement }),
  ).not.toBeChecked()
  expect(screen.getByRole('button', { name: '选择候选 1' })).toBeDisabled()
  await userEvent.click(screen.getByRole('checkbox', { name: acknowledgement }))
  rerender(
    <ProjectActions
      project={project({
        revision: 2,
        candidate_ids: ['c2'],
        candidate_checks: { c2: check },
      })}
      onRefresh={refresh}
    />,
  )
  expect(
    screen.getByRole('checkbox', { name: acknowledgement }),
  ).not.toBeChecked()
  await userEvent.click(screen.getByRole('checkbox', { name: acknowledgement }))
  await userEvent.selectOptions(screen.getByLabelText('金属效果'), 'silver')
  await userEvent.click(screen.getByRole('button', { name: '恢复当前参数' }))
  expect(
    screen.getByRole('checkbox', { name: acknowledgement }),
  ).not.toBeChecked()
  expect(screen.getByRole('button', { name: '选择候选 1' })).toBeDisabled()
})
it('clears the first acknowledgement when comparing another candidate', async () => {
  render(
    <ProjectActions
      project={project({
        candidate_ids: ['c1', 'c2'],
        candidate_checks: { c1: check, c2: check },
      })}
      onRefresh={vi.fn()}
    />,
  )
  await userEvent.click(
    screen.getAllByRole('checkbox', { name: acknowledgement })[0],
  )
  await userEvent.click(screen.getByRole('button', { name: '对照候选 2' }))
  expect(
    screen.getAllByRole('checkbox', { name: acknowledgement })[0],
  ).not.toBeChecked()
})
it('compares original and candidate without stretching and controls overlay opacity', async () => {
  render(<ProjectActions project={project()} onRefresh={vi.fn()} />)
  await userEvent.click(screen.getByRole('button', { name: '对照候选 1' }))
  const original = screen.getByRole('img', { name: '对照原图' })
  const candidate = screen.getByRole('img', { name: '对照候选效果' })
  expect(original).toHaveAttribute('src', '/api/v1/nmm/assets/original')
  expect(candidate).toHaveAttribute('src', '/api/v1/nmm/assets/c1')
  expect(original.parentElement).toHaveStyle({ aspectRatio: '1200 / 800' })
  expect(candidate.parentElement).toHaveStyle({ aspectRatio: '1200 / 800' })
  await userEvent.click(screen.getByRole('button', { name: '透明叠加' }))
  const slider = screen.getByRole('slider', { name: '候选不透明度' })
  expect(slider).toHaveValue('50')
  fireEvent.change(slider, { target: { value: '25' } })
  expect(screen.getByRole('img', { name: '对照候选效果' })).toHaveStyle({
    opacity: '0.25',
  })
  await userEvent.click(screen.getByRole('button', { name: '并排对照' }))
  expect(screen.queryByRole('slider')).not.toBeInTheDocument()
})
it.each([undefined, { c1: { ...check, status: 'blocked' as const } }])(
  'blocks selected-candidate recovery without an eligible check',
  async (candidate_checks) => {
    render(
      <ProjectActions
        project={project({
          status: 'stopped',
          selected_candidate_id: 'c1',
          candidate_checks,
        })}
        onRefresh={vi.fn()}
      />,
    )
    const resume = screen.getByRole('button', { name: '估算并继续任务' })
    expect(resume).toBeDisabled()
    await userEvent.click(resume)
    expect(nmmApi.estimate).not.toHaveBeenCalled()
    expect(nmmApi.run).not.toHaveBeenCalled()
  },
)
it('explains how to restart when a selected candidate is blocked', () => {
  render(
    <ProjectActions
      project={project({
        status: 'failed',
        selected_candidate_id: 'c1',
        candidate_checks: { c1: { ...check, status: 'blocked' } },
      })}
      onRefresh={vi.fn()}
    />,
  )
  expect(
    screen.getByText('此候选不能继续；请返回项目列表新建项目并重新生成。'),
  ).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '估算并继续任务' })).toBeDisabled()
  expect(
    screen.queryByRole('button', { name: '重新估算候选' }),
  ).not.toBeInTheDocument()
  expect(nmmApi.estimate).not.toHaveBeenCalled()
})
it('requires human review again before recovering a selected candidate', async () => {
  vi.mocked(nmmApi.run).mockRejectedValueOnce(new NmmApiError(0, '中断'))
  render(
    <ProjectActions
      project={project({ status: 'stopped', selected_candidate_id: 'c1' })}
      onRefresh={vi.fn()}
    />,
  )
  expect(screen.getByRole('button', { name: '估算并继续任务' })).toBeDisabled()
  await userEvent.click(screen.getByRole('checkbox', { name: acknowledgement }))
  await userEvent.click(screen.getByRole('button', { name: '估算并继续任务' }))
  await userEvent.click(
    await screen.findByRole('button', { name: '批准并继续' }),
  )
  const retry = await screen.findByRole('button', { name: '重试原操作' })
  const body = vi.mocked(nmmApi.run).mock.calls[0][1]
  expect(body).toMatchObject({
    quality_acknowledged: true,
    confirm_generation: false,
  })
  await userEvent.click(retry)
  await waitFor(() => expect(nmmApi.run).toHaveBeenCalledTimes(2))
  expect(vi.mocked(nmmApi.run).mock.calls[1][1]).toEqual(body)
})
it('rechecks eligibility if a candidate becomes blocked while a quote is open', async () => {
  const refresh = vi.fn()
  const { rerender } = render(
    <ProjectActions project={project()} onRefresh={refresh} />,
  )
  await userEvent.click(screen.getByRole('checkbox', { name: acknowledgement }))
  await userEvent.click(screen.getByRole('button', { name: '选择候选 1' }))
  await screen.findByRole('button', { name: '批准并继续' })
  rerender(
    <ProjectActions
      project={project({
        candidate_checks: { c1: { ...check, status: 'blocked' as const } },
      })}
      onRefresh={refresh}
    />,
  )
  const approve = screen.queryByRole('button', { name: '批准并继续' })
  if (approve) await userEvent.click(approve)
  expect(nmmApi.confirm).not.toHaveBeenCalled()
})
