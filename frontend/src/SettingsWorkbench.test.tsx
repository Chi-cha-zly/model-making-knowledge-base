import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import SettingsWorkbench from './pages/settings/SettingsPage'
const profile = {
  id: 'one',
  name: '工作模型',
  base_url: 'https://example.com/v1',
  model: 'model-one',
  compare_model: '',
  reasoning_effort: 'auto',
  max_output_tokens: 2048,
  has_api_key: true,
  read_only: false,
}
let state: any
let writes: any[]
beforeEach(() => {
  writes = []
  state = {
    profiles: [
      { ...profile, id: 'env', name: '环境配置', read_only: true },
      profile,
    ],
    active_profile_id: 'one',
    custom_prompt: '',
    base_prompt: '固定引用规则',
    prompt_presets: [{ id: 'concise', name: '简洁回答', text: '请简洁回答。' }],
    storage_notice: '密钥保存在系统凭据库，不包含在普通备份。',
  }
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url, init) => {
      const body = init?.body ? JSON.parse(init.body) : undefined
      if (init?.method === 'PUT' && String(url).includes('/profiles/')) {
        writes.push(body)
        state = {
          ...state,
          profiles: state.profiles.map((p: any) =>
            p.id === 'one' ? { ...p, ...body, api_key: undefined } : p,
          ),
        }
      }
      if (String(url).endsWith('/prompt')) {
        writes.push(body)
        state = { ...state, custom_prompt: body.text }
      }
      if (String(url).endsWith('/test'))
        return new Response(
          JSON.stringify({ ok: true, message: '连接成功', latency_ms: 12 }),
        )
      return new Response(JSON.stringify(state))
    }),
  )
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})
it('keeps a saved key out of the form and omits blank key on save', async () => {
  const user = userEvent.setup()
  render(<SettingsWorkbench />)
  const name = await screen.findByLabelText('连接名称')
  await user.clear(name)
  await user.type(name, '新名称')
  expect(screen.getByLabelText('API Key')).toHaveValue('')
  await user.click(screen.getByRole('button', { name: '保存连接' }))
  await waitFor(() => expect(writes).toHaveLength(1))
  expect(writes[0]).not.toHaveProperty('api_key')
  expect(await screen.findByText('连接已保存')).toBeInTheDocument()
})
it('requires replacement or explicit clear for endpoint change and clears draft on selection', async () => {
  const user = userEvent.setup()
  render(<SettingsWorkbench />)
  const address = await screen.findByLabelText('接口根地址')
  await user.clear(address)
  await user.type(address, 'https://other.example/v1')
  await user.click(screen.getByRole('button', { name: '保存连接' }))
  expect(screen.getByRole('alert')).toHaveTextContent('更换接口地址')
  expect(writes).toHaveLength(0)
  await user.type(screen.getByLabelText('API Key'), 'draft-secret')
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  await user.selectOptions(screen.getByLabelText('选择连接'), 'env')
  expect(screen.queryByDisplayValue('draft-secret')).not.toBeInTheDocument()
  expect(screen.getByLabelText('连接名称')).toBeDisabled()
  expect(
    screen.getByText('密钥来自只读环境配置，不会回显。'),
  ).toBeInTheDocument()
})
it('previews presets and reset as drafts until saved', async () => {
  const user = userEvent.setup()
  render(<SettingsWorkbench />)
  await user.click(await screen.findByRole('button', { name: '简洁回答' }))
  expect(screen.getByLabelText('补充系统提示词')).toHaveValue('请简洁回答。')
  expect(writes).toHaveLength(0)
  await user.click(screen.getByText('预览生效提示词'))
  expect(screen.getByTestId('prompt-preview')).toHaveTextContent('固定引用规则')
  expect(screen.getByTestId('prompt-preview')).toHaveTextContent('请简洁回答。')
  await user.click(screen.getByRole('button', { name: '保存提示词' }))
  await waitFor(() => expect(writes).toEqual([{ text: '请简洁回答。' }]))
  await user.click(screen.getByRole('button', { name: '恢复默认' }))
  expect(screen.getByLabelText('补充系统提示词')).toHaveValue('')
  expect(writes).toHaveLength(1)
})
it('only tests a saved unchanged connection after explicit action', async () => {
  const user = userEvent.setup()
  render(<SettingsWorkbench />)
  await user.click(
    await screen.findByRole('button', { name: '测试已保存连接' }),
  )
  expect(await screen.findByText(/连接成功.*12/)).toBeInTheDocument()
  await user.type(screen.getByLabelText('API Key'), 'draft')
  expect(screen.getByRole('button', { name: '测试已保存连接' })).toBeDisabled()
})
it('explicitly clears the saved key and disables profile switching during a save', async () => {
  const user = userEvent.setup()
  render(<SettingsWorkbench />)
  await user.click(await screen.findByLabelText('保存时清除密钥'))
  let finish: ((value: Response) => void) | undefined
  const previous = vi.mocked(fetch).getMockImplementation()!
  vi.mocked(fetch).mockImplementation((url, init) =>
    init?.method === 'PUT' && String(url).includes('/profiles/')
      ? new Promise((resolve) => {
          writes.push(JSON.parse(String(init.body)))
          finish = resolve
        })
      : previous(url, init),
  )
  await user.click(screen.getByRole('button', { name: '保存连接' }))
  expect(screen.getByLabelText('选择连接')).toBeDisabled()
  expect(writes[0].api_key).toBe('')
  finish?.(new Response(JSON.stringify(state)))
  expect(await screen.findByText('连接已保存')).toBeInTheDocument()
  expect(screen.getByLabelText('API Key')).toHaveValue('')
})
it('keeps the profile draft after save failure and never renders raw error bodies', async () => {
  const user = userEvent.setup()
  render(<SettingsWorkbench />)
  await user.type(await screen.findByLabelText('API Key'), 'temporary-secret')
  vi.mocked(fetch).mockResolvedValue(
    new Response(JSON.stringify({ detail: 'temporary-secret upstream body' }), {
      status: 422,
    }),
  )
  await user.click(screen.getByRole('button', { name: '保存连接' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('设置未通过校验')
  expect(screen.getByLabelText('API Key')).toHaveValue('temporary-secret')
  expect(
    screen.queryByText(/temporary-secret upstream/),
  ).not.toBeInTheDocument()
  expect(screen.getByLabelText('选择连接')).not.toBeDisabled()
})
it('requires explicit clear when changing an existing local endpoint without a saved key', async () => {
  state.profiles = state.profiles.map((p: any) =>
    p.id === 'one'
      ? { ...p, base_url: 'http://localhost:9000/v1', has_api_key: false }
      : p,
  )
  const user = userEvent.setup()
  render(<SettingsWorkbench />)
  const address = await screen.findByLabelText('接口根地址')
  await user.clear(address)
  await user.type(address, 'http://localhost:9001/v1')
  await user.click(screen.getByRole('button', { name: '保存连接' }))
  expect(screen.getByRole('alert')).toHaveTextContent('更换接口地址')
  expect(writes).toHaveLength(0)
  await user.click(screen.getByLabelText('保存时清除密钥'))
  await user.click(screen.getByRole('button', { name: '保存连接' }))
  await waitFor(() => expect(writes).toHaveLength(1))
  expect(writes[0]).toMatchObject({
    base_url: 'http://localhost:9001/v1',
    api_key: '',
  })
})

it.each([
  ['saved_unverified', '密钥已保存，尚未验证系统凭据是否可读取。'],
  ['available', '系统凭据可读取；服务连接仍需单独测试。'],
  ['missing', '系统凭据已缺失，请重新输入 API key 并保存。'],
  ['unavailable', '系统凭据库不可用，请解锁后重试；不会改用明文或环境密钥。'],
])(
  'shows credential state %s separately from saved-key presence',
  async (status, message) => {
    state.profiles = state.profiles.map((p: any) =>
      p.id === 'one' ? { ...p, credential_status: status } : p,
    )
    render(<SettingsWorkbench />)
    expect(await screen.findByText(message)).toBeInTheDocument()
    expect(screen.getByLabelText('API Key')).toHaveValue('')
  },
)

it('shows pending durability and cleanup while keeping the saved connection editable', async () => {
  state.credential_cleanup_pending = true
  state.credential_durability_pending = true
  render(<SettingsWorkbench />)
  expect(
    await screen.findByText(/设置已更新，但磁盘持久化确认未完成/),
  ).toBeInTheDocument()
  expect(screen.getByText(/旧凭据清理尚未完成/)).toBeInTheDocument()
  expect(screen.getByLabelText('连接名称')).not.toBeDisabled()
})

it('explains vault unavailability without rendering backend errors or secrets', async () => {
  const user = userEvent.setup()
  render(<SettingsWorkbench />)
  await user.type(await screen.findByLabelText('API Key'), 'temporary-secret')
  vi.mocked(fetch).mockResolvedValue(
    new Response(JSON.stringify({ detail: 'temporary-secret upstream' }), {
      status: 503,
    }),
  )
  await user.click(screen.getByRole('button', { name: '保存连接' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('系统凭据')
  expect(
    screen.queryByText(/temporary-secret upstream/),
  ).not.toBeInTheDocument()
})
