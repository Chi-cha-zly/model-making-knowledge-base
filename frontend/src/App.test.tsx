import {
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { StrictMode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'

const source = {
  id: 1,
  title: '郡是涂料官方说明',
  url: 'https://example.com/guide',
  author: '郡是',
  license_note: '',
}
const source2 = {
  id: 4,
  title: '个人测试记录',
  url: '',
  author: '自己',
  license_note: '',
}
const technique = {
  id: 2,
  title: '喷涂底漆前的表面清理',
  category: '涂装',
  summary: '去除粉尘和油脂',
  content_blocks: [
    { type: 'paragraph', text: '清洗部件' },
    { type: 'paragraph', text: '充分晾干' },
  ],
  source_ids: [1],
  tags: ['底漆'],
  status: 'published',
}
const product = {
  id: 3,
  brand: 'Mr.Hobby',
  name: '水性底漆',
  sku: 'H-001',
  kind: '底漆',
  aliases: ['水补土'],
  source_ids: [1],
  status: 'published',
}

function response(body: unknown, status = 200) {
  return Promise.resolve(
    new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    }),
  )
}

beforeEach(() => {
  window.history.replaceState(null, '', '/')
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/search'))
        return response({
          techniques: decodeURIComponent(url).includes('底漆')
            ? [technique]
            : [],
          products: decodeURIComponent(url).includes('底漆') ? [product] : [],
        })
      if (url.endsWith('/techniques') && init?.method === 'POST')
        return response(
          { ...technique, ...JSON.parse(String(init.body)), id: 7 },
          201,
        )
      if (url.endsWith('/attachments') && init?.method === 'POST')
        return response(
          {
            id: 8,
            filename: 'photo.png',
            content_type: 'image/png',
            url: '/api/v1/attachments/8',
          },
          201,
        )
      if (url.endsWith('/techniques/2') && init?.method === 'PATCH')
        return response({ ...technique, ...JSON.parse(String(init.body)) })
      if (url.endsWith('/sources/1') && init?.method === 'PATCH')
        return response({ ...source, ...JSON.parse(String(init.body)) })
      if (url.endsWith('/sources/1') && init?.method === 'DELETE')
        return response({ ...source, status: 'withdrawn' })
      if (url.endsWith('/sources')) return response([source, source2])
      if (url.endsWith('/techniques')) return response([technique])
      if (url.endsWith('/products')) return response([product])
      return response({}, 404)
    }),
  )
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('workbench', () => {
  it('opens the management deep link and follows hash history changes', async () => {
    window.history.replaceState(null, '', '/#manage')
    render(<App />)
    expect(
      await screen.findByRole('heading', { name: '让每条记录有据可查。' }),
    ).toBeInTheDocument()
    window.location.hash = '/'
    expect(
      await screen.findByRole('searchbox', { name: '搜索资料' }),
    ).toBeInTheDocument()
  })

  it('deduplicates catalogue loading under StrictMode', async () => {
    render(
      <StrictMode>
        <App />
      </StrictMode>,
    )
    await screen.findByText('喷涂底漆前的表面清理')
    expect(
      vi
        .mocked(fetch)
        .mock.calls.filter(([url]) => String(url).endsWith('/sources')),
    ).toHaveLength(1)
  })

  it('reads an empty blocks-only technique without legacy steps', async () => {
    const fetcher = vi.mocked(fetch).getMockImplementation()!
    vi.mocked(fetch).mockImplementation((input, init) =>
      String(input).endsWith('/techniques')
        ? response([{ ...technique, content_blocks: [] }])
        : fetcher(input, init),
    )
    const user = userEvent.setup()
    render(<App />)
    await user.click(
      await screen.findByRole('button', { name: /喷涂底漆前的表面清理/ }),
    )
    expect(screen.getByRole('region', { name: '资料详情' })).toHaveTextContent(
      '暂无正文',
    )
  })

  it('searches both techniques and products and filters the displayed category', async () => {
    const user = userEvent.setup()
    render(<App />)
    await user.type(screen.getByRole('searchbox', { name: '搜索资料' }), '底漆')
    await user.click(screen.getByRole('button', { name: '搜索' }))
    expect(await screen.findByText('喷涂底漆前的表面清理')).toBeInTheDocument()
    expect(screen.getByText('H-001')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '只看产品' }))
    expect(screen.queryByText('喷涂底漆前的表面清理')).not.toBeInTheDocument()
    expect(screen.getByText('H-001')).toBeInTheDocument()
  })

  it('shows a selected record with its original source link', async () => {
    const user = userEvent.setup()
    render(<App />)
    await user.click(
      await screen.findByRole('button', { name: /喷涂底漆前的表面清理/ }),
    )
    const detail = screen.getByRole('region', { name: '资料详情' })
    expect(within(detail).getByText('清洗部件')).toBeInTheDocument()
    expect(
      within(detail).getByRole('link', { name: /郡是涂料官方说明/ }),
    ).toHaveAttribute('href', source.url)
  })

  it('saves a new technique from the editor', async () => {
    const user = userEvent.setup()
    render(<App />)
    await user.click(screen.getByRole('button', { name: '新建' }))
    await user.click(screen.getByRole('menuitem', { name: '新建技巧' }))
    await user.type(screen.getByRole('textbox', { name: '标题' }), '接缝处理')
    await user.type(
      screen.getByRole('textbox', { name: '正文' }),
      '先假组再上胶',
    )
    await user.click(screen.getByRole('radio', { name: '已有资料' }))
    await user.click(screen.getByRole('checkbox', { name: source.title }))
    await user.click(screen.getByRole('button', { name: '保存技巧' }))
    await waitFor(() =>
      expect(screen.getByText('已保存技巧')).toBeInTheDocument(),
    )
    const calls = vi.mocked(fetch).mock.calls
    expect(
      calls.some(
        ([url, options]) =>
          String(url).endsWith('/techniques') &&
          options?.method === 'POST' &&
          JSON.parse(String(options.body)).title === '接缝处理',
      ),
    ).toBe(true)
  })

  it('attaches an uploaded image to the selected technique', async () => {
    const user = userEvent.setup()
    render(<App />)
    await user.click(
      await screen.findByRole('button', { name: /喷涂底漆前的表面清理/ }),
    )
    await user.upload(
      screen.getByLabelText('上传图片'),
      new File(['image'], 'photo.png', { type: 'image/png' }),
    )
    await waitFor(() =>
      expect(
        vi
          .mocked(fetch)
          .mock.calls.some(
            ([url, options]) =>
              String(url).endsWith('/techniques/2') &&
              options?.method === 'PATCH' &&
              JSON.parse(String(options.body)).attachment_ids?.includes(8),
          ),
      ).toBe(true),
    )
    expect(
      await screen.findByRole('link', { name: 'photo.png' }),
    ).toHaveAttribute('href', '/api/v1/attachments/8')
  })

  it('preserves all linked sources when editing a technique', async () => {
    const user = userEvent.setup()
    vi.mocked(fetch).mockImplementation(
      (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        if (url.endsWith('/sources')) return response([source, source2])
        if (url.endsWith('/techniques'))
          return response([{ ...technique, source_ids: [1, 4] }])
        if (url.endsWith('/products')) return response([product])
        if (url.endsWith('/techniques/2') && init?.method === 'PATCH')
          return response({ ...technique, ...JSON.parse(String(init.body)) })
        return response({}, 404)
      },
    )
    render(<App />)
    await user.click(
      await screen.findByRole('button', { name: /喷涂底漆前的表面清理/ }),
    )
    await user.click(screen.getByRole('button', { name: '编辑' }))
    expect(screen.getByRole('checkbox', { name: source.title })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: source2.title })).toBeChecked()
    await user.click(screen.getByRole('button', { name: '保存技巧' }))
    await waitFor(() =>
      expect(
        vi
          .mocked(fetch)
          .mock.calls.some(
            ([url, options]) =>
              String(url).endsWith('/techniques/2') &&
              options?.method === 'PATCH' &&
              JSON.stringify(JSON.parse(String(options.body)).source_ids) ===
                '[1,4]',
          ),
      ).toBe(true),
    )
  })

  it('preserves all linked sources when editing a product', async () => {
    const user = userEvent.setup()
    vi.mocked(fetch).mockImplementation(
      (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        if (url.endsWith('/sources')) return response([source, source2])
        if (url.endsWith('/techniques')) return response([technique])
        if (url.endsWith('/products'))
          return response([{ ...product, source_ids: [1, 4] }])
        if (url.endsWith('/products/3') && init?.method === 'PATCH')
          return response({ ...product, ...JSON.parse(String(init.body)) })
        return response({}, 404)
      },
    )
    render(<App />)
    await user.click(
      await screen.findByRole('button', { name: /Mr.Hobby 水性底漆/ }),
    )
    await user.click(screen.getByRole('button', { name: '编辑' }))
    expect(screen.getByRole('checkbox', { name: source.title })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: source2.title })).toBeChecked()
    await user.click(screen.getByRole('button', { name: '保存产品' }))
    await waitFor(() =>
      expect(
        vi
          .mocked(fetch)
          .mock.calls.some(
            ([url, options]) =>
              String(url).endsWith('/products/3') &&
              options?.method === 'PATCH' &&
              JSON.stringify(JSON.parse(String(options.body)).source_ids) ===
                '[1,4]',
          ),
      ).toBe(true),
    )
  })

  it('hides drafts, withdrawn records, and records with withdrawn sources from the browse index', async () => {
    vi.mocked(fetch).mockImplementation((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/sources'))
        return response([source, { ...source2, status: 'withdrawn' }])
      if (url.endsWith('/techniques'))
        return response([
          technique,
          { ...technique, id: 5, title: '草稿技巧', status: 'draft' },
          { ...technique, id: 6, title: '撤回技巧', status: 'withdrawn' },
        ])
      if (url.endsWith('/products'))
        return response([
          { ...product, source_ids: [4] },
          { ...product, id: 7, name: '仍有有效来源的底漆', source_ids: [1, 4] },
        ])
      return response({}, 404)
    })
    render(<App />)
    expect(
      await screen.findByRole('button', { name: /喷涂底漆前的表面清理/ }),
    ).toBeInTheDocument()
    expect(screen.queryByText('草稿技巧')).not.toBeInTheDocument()
    expect(screen.queryByText('撤回技巧')).not.toBeInTheDocument()
    expect(screen.queryByText('Mr.Hobby 水性底漆')).not.toBeInTheDocument()
    expect(screen.getByText('Mr.Hobby 仍有有效来源的底漆')).toBeInTheDocument()
  })

  it('edits and withdraws a registered source', async () => {
    const user = userEvent.setup()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<App />)
    await user.click(screen.getByRole('button', { name: '资料管理' }))
    await user.click(
      await screen.findByRole('button', { name: '编辑郡是涂料官方说明' }),
    )
    const dialog = screen.getByRole('dialog', { name: '编辑资料来源' })
    await user.clear(within(dialog).getByRole('textbox', { name: '来源标题' }))
    await user.type(
      within(dialog).getByRole('textbox', { name: '来源标题' }),
      '修订后的来源',
    )
    await user.click(screen.getByRole('button', { name: '保存来源' }))
    await waitFor(() =>
      expect(
        vi
          .mocked(fetch)
          .mock.calls.some(
            ([url, options]) =>
              String(url).endsWith('/sources/1') &&
              options?.method === 'PATCH' &&
              JSON.parse(String(options.body)).title === '修订后的来源',
          ),
      ).toBe(true),
    )
    await user.click(
      screen.getByRole('button', { name: '撤回郡是涂料官方说明' }),
    )
    await waitFor(() =>
      expect(
        vi
          .mocked(fetch)
          .mock.calls.some(
            ([url, options]) =>
              String(url).endsWith('/sources/1') &&
              options?.method === 'DELETE',
          ),
      ).toBe(true),
    )
    vi.restoreAllMocks()
  })

  it('requires explicit removal of an inactive linked source before saving', async () => {
    const user = userEvent.setup()
    vi.mocked(fetch).mockImplementation(
      (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        if (url.endsWith('/sources'))
          return response([source, { ...source2, status: 'withdrawn' }])
        if (url.endsWith('/techniques'))
          return response([{ ...technique, source_ids: [1, 4] }])
        if (url.endsWith('/products')) return response([])
        if (url.endsWith('/techniques/2') && init?.method === 'PATCH')
          return response({ ...technique, ...JSON.parse(String(init.body)) })
        return response({}, 404)
      },
    )
    render(<App />)
    await user.click(
      await screen.findByRole('button', { name: /喷涂底漆前的表面清理/ }),
    )
    await user.click(screen.getByRole('button', { name: '编辑' }))
    const dialog = screen.getByRole('dialog', { name: '编辑技巧' })
    expect(
      within(dialog).getByRole('checkbox', { name: source.title }),
    ).toBeChecked()
    expect(
      within(dialog).queryByRole('checkbox', { name: source2.title }),
    ).not.toBeInTheDocument()
    expect(within(dialog).getByText(/个人测试记录.*已撤回/)).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: '保存技巧' }))
    expect(within(dialog).getByRole('alert')).toHaveTextContent(
      '移除或替换不可用来源',
    )
    expect(
      vi
        .mocked(fetch)
        .mock.calls.some(
          ([url, options]) =>
            String(url).endsWith('/techniques/2') &&
            options?.method === 'PATCH',
        ),
    ).toBe(false)
    await user.click(
      within(dialog).getByRole('button', { name: `移除${source2.title}` }),
    )
    await user.click(within(dialog).getByRole('button', { name: '保存技巧' }))
    await waitFor(() =>
      expect(
        vi
          .mocked(fetch)
          .mock.calls.some(
            ([url, options]) =>
              String(url).endsWith('/techniques/2') &&
              options?.method === 'PATCH' &&
              JSON.stringify(JSON.parse(String(options.body)).source_ids) ===
                '[1]',
          ),
      ).toBe(true),
    )
  })

  it('explains when no published source can be selected', async () => {
    const user = userEvent.setup()
    vi.mocked(fetch).mockImplementation((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/sources'))
        return response([
          { ...source, status: 'draft' },
          { ...source2, status: 'withdrawn' },
        ])
      if (url.endsWith('/techniques') || url.endsWith('/products'))
        return response([])
      return response({}, 404)
    })
    render(<App />)
    await user.click(screen.getByRole('button', { name: '新建' }))
    await user.click(screen.getByRole('menuitem', { name: '新建技巧' }))
    const dialog = screen.getByRole('dialog', { name: '新建技巧' })
    await user.click(within(dialog).getByRole('radio', { name: '已有资料' }))
    expect(
      within(dialog).getByText('没有已发布来源。请先登记或发布一条来源。'),
    ).toBeInTheDocument()
    expect(within(dialog).queryAllByRole('checkbox')).toHaveLength(0)
    expect(
      within(dialog).getByRole('button', { name: '保存技巧' }),
    ).toBeDisabled()
  })
})

it('keeps settings navigation in the URL and restores it after a reload', async () => {
  window.history.replaceState(null, '', '/#evaluation-experiments')
  const user = userEvent.setup()
  const first = render(<App />)
  await user.click(screen.getByRole('button', { name: '设置' }))
  await waitFor(() => expect(window.location.hash).toBe('#/settings'))
  expect(screen.getByRole('region', { name: '应用设置' })).toBeInTheDocument()
  first.unmount()
  render(<App />)
  expect(screen.getByRole('region', { name: '应用设置' })).toBeInTheDocument()
  window.history.replaceState(null, '', '/')
})

it('publishes personal original techniques without a pre-existing source or withdrawn option', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === 'POST')
        return response({ ...technique, ...JSON.parse(String(init.body)) }, 201)
      return response([])
    }),
  )
  const user = userEvent.setup()
  render(<App />)
  await user.click(screen.getByRole('button', { name: '新建' }))
  await user.click(screen.getByRole('menuitem', { name: '新建技巧' }))
  const dialog = screen.getByRole('dialog', { name: '新建技巧' })
  expect(
    within(dialog).queryByRole('combobox', { name: '状态' }),
  ).not.toBeInTheDocument()
  await user.click(within(dialog).getByRole('radio', { name: '个人原创' }))
  expect(
    within(dialog).queryByRole('group', { name: '资料来源' }),
  ).not.toBeInTheDocument()
  await user.type(
    within(dialog).getByRole('textbox', { name: '标题' }),
    '原创方法',
  )
  await user.click(within(dialog).getByRole('button', { name: '保存技巧' }))
  expect(await screen.findByText('已保存技巧')).toBeInTheDocument()
  const call = vi
    .mocked(fetch)
    .mock.calls.find(([, options]) => options?.method === 'POST')
  expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({
    original: true,
    source_ids: [],
    status: 'published',
  })
})

it('withdraws a published technique and edits it without republishing until explicit restore', async () => {
  let current = { ...technique }
  vi.stubGlobal(
    'confirm',
    vi.fn(() => true),
  )
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/techniques/2')) {
        if (init?.method === 'DELETE')
          current = { ...current, status: 'withdrawn' }
        if (init?.method === 'PATCH')
          current = { ...current, ...JSON.parse(String(init.body)) }
        return response(current)
      }
      if (url.endsWith('/sources')) return response([source])
      if (url.endsWith('/techniques')) return response([current])
      if (url.endsWith('/products')) return response([])
      return response({}, 404)
    }),
  )
  const user = userEvent.setup()
  render(<App />)
  await user.click(
    await screen.findByRole('button', { name: /喷涂底漆前的表面清理/ }),
  )
  await user.click(screen.getByRole('button', { name: '撤回已发布技巧' }))
  expect(await screen.findByText('技巧已撤回')).toBeInTheDocument()
  expect(
    screen.queryByRole('button', { name: /喷涂底漆前的表面清理/ }),
  ).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: '资料管理' }))
  await user.click(
    screen.getByRole('button', { name: `编辑技巧${technique.title}` }),
  )
  const dialog = screen.getByRole('dialog', { name: '编辑技巧' })
  expect(
    within(dialog).queryByRole('combobox', { name: '状态' }),
  ).not.toBeInTheDocument()
  await user.click(within(dialog).getByRole('button', { name: '保存技巧' }))
  expect(await screen.findByText('已保存技巧')).toBeInTheDocument()
  expect(current.status).toBe('withdrawn')
  await user.click(screen.getByRole('button', { name: '资料管理' }))
  await user.click(
    screen.getByRole('button', { name: `恢复发布${technique.title}` }),
  )
  expect(await screen.findByText('技巧已恢复发布')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: '资料检索' }))
  expect(
    await screen.findByRole('button', { name: /喷涂底漆前的表面清理/ }),
  ).toBeInTheDocument()
})
