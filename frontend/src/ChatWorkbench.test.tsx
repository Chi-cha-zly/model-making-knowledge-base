import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import App from './App'

let configured = true
let runs: any[] = []
let messages: any[] = []
let conversationList: any[] = []
let conversation = {
  id: 1,
  title: '我的制作计划',
  created_at: '',
  updated_at: '',
}
class Stream {
  static all: Stream[] = []
  listeners: Record<string, (event: any) => void> = {}
  onerror: (() => void) | null = null
  onopen: (() => void) | null = null
  closed = false
  constructor(public url: string) {
    Stream.all.push(this)
  }
  addEventListener(name: string, callback: any) {
    this.listeners[name] = callback
  }
  close() {
    this.closed = true
  }
  snapshot(run: any) {
    this.listeners.snapshot?.({ data: JSON.stringify(run) })
  }
}
function reply(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status }))
}
function newRun(content: string, retry = false) {
  if (!retry)
    messages.push({
      id: messages.length + 1,
      conversation_id: 1,
      role: 'user',
      content,
    })
  const userId = [...messages].reverse().find((m) => m.role === 'user').id
  messages.push({
    id: messages.length + 1,
    conversation_id: 1,
    role: 'assistant',
    content: '',
  })
  const run = {
    id: runs.length + 1,
    conversation_id: 1,
    user_message_id: userId,
    assistant_message_id: messages.length,
    status: 'running',
    content: '',
    error: null,
    citations: [],
  }
  runs.push(run)
  return run
}
beforeEach(() => {
  configured = true
  conversation = {
    id: 1,
    title: '我的制作计划',
    created_at: '',
    updated_at: '',
  }
  conversationList = [conversation]
  runs = []
  messages = []
  Stream.all = []
  window.history.replaceState(null, '', '/')
  vi.stubGlobal('EventSource', Stream)
  vi.stubGlobal(
    'fetch',
    vi.fn((input: any, init?: RequestInit) => {
      const url = String(input)
      const body = init?.body ? JSON.parse(String(init.body)) : {}
      if (url.endsWith('/agent/config'))
        return reply({
          configured,
          model: configured ? 'test-model' : null,
          message: '请配置模型服务',
        })
      if (url.endsWith('/conversations')) {
        if (init?.method === 'POST') {
          conversationList = [conversation]
          return reply(conversation)
        }
        return reply(conversationList)
      }
      if (url.endsWith('/conversations/1')) {
        if (init?.method === 'DELETE') {
          conversationList = []
          return Promise.resolve(new Response(null, { status: 204 }))
        }
        if (init?.method === 'PATCH') {
          conversation = { ...conversation, title: body.title }
          conversationList = [conversation]
        }
        return reply({ ...conversation, messages, runs })
      }
      if (url.endsWith('/conversations/1/runs'))
        return reply(newRun(body.content))
      if (url.endsWith('/stop')) {
        runs[runs.length - 1].status = 'stopped'
        return reply(runs[runs.length - 1])
      }
      if (url.endsWith('/retry')) return reply(newRun('', true))
      if (url.endsWith('/steer')) {
        runs[runs.length - 1].status = 'stopped'
        return reply(newRun(body.content))
      }
      if (/\/runs\/\d+$/.test(url)) return reply(runs[runs.length - 1])
      return reply([])
    }),
  )
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})
async function openChat() {
  const user = userEvent.setup()
  render(<App />)
  await user.click(screen.getByRole('button', { name: '对话助手' }))
  await screen.findByRole('button', { name: '我的制作计划' })
  await user.click(screen.getByRole('button', { name: '我的制作计划' }))
  await waitFor(() =>
    expect(
      screen.getByRole('textbox', { name: '制作问题或补充要求' }),
    ).toBeEnabled(),
  )
  return user
}
it('replaces replayed stream snapshots and retains partial output after stopping', async () => {
  const user = await openChat()
  await user.type(
    screen.getByRole('textbox', { name: '制作问题或补充要求' }),
    '怎样处理水口？',
  )
  await user.click(screen.getByRole('button', { name: '发送' }))
  await waitFor(() => expect(Stream.all.length).toBeGreaterThan(0))
  runs[0].content = '先剪下零件'
  act(() => {
    Stream.all.at(-1)!.snapshot(runs[0])
    Stream.all.at(-1)!.snapshot(runs[0])
  })
  expect(screen.getByText('先剪下零件')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: '停止生成' }))
  expect(await screen.findByText('已停止')).toBeInTheDocument()
  expect(screen.getByText('先剪下零件')).toBeInTheDocument()
  expect(Stream.all.at(-1)!.closed).toBe(true)
})
it('retries without duplicating the user question and steers with a new instruction', async () => {
  messages = [
    { id: 1, role: 'user', content: '喷涂建议' },
    { id: 2, role: 'assistant', content: '部分回答' },
  ]
  runs = [
    {
      id: 1,
      conversation_id: 1,
      user_message_id: 1,
      assistant_message_id: 2,
      status: 'failed',
      content: '部分回答',
      error: '上游断开',
      citations: [],
    },
  ]
  const user = await openChat()
  await user.click(screen.getByRole('button', { name: '重试回答' }))
  await screen.findByRole('button', { name: '停止生成' })
  expect(screen.getAllByText('喷涂建议')).toHaveLength(1)
  await user.type(
    screen.getByRole('textbox', { name: '制作问题或补充要求' }),
    '我只有水性漆',
  )
  await user.click(screen.getByRole('button', { name: '打断并发送' }))
  expect(await screen.findByText('我只有水性漆')).toBeInTheDocument()
  expect(screen.getAllByText('喷涂建议')).toHaveLength(1)
})
it('blocks model requests when unconfigured while allowing an editable guided draft', async () => {
  configured = false
  const user = await openChat()
  expect(await screen.findByText('请配置模型服务')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: '整理制作需求' }))
  await user.type(screen.getByRole('textbox', { name: '模型类型' }), '高达拼装')
  await user.type(screen.getByRole('textbox', { name: '制作目标' }), '干净素组')
  await user.click(screen.getByRole('button', { name: '填入草稿' }))
  expect(
    (
      screen.getByRole('textbox', {
        name: '制作问题或补充要求',
      }) as HTMLTextAreaElement
    ).value,
  ).toContain('高达拼装')
  expect(screen.getByRole('button', { name: '发送' })).toBeDisabled()
})

it('restores an active conversation after refresh and reconnects using full snapshots', async () => {
  messages = [
    { id: 1, role: 'user', content: '接着制作' },
    { id: 2, role: 'assistant', content: '已保存的部分回答' },
  ]
  runs = [
    {
      id: 1,
      conversation_id: 1,
      user_message_id: 1,
      assistant_message_id: 2,
      status: 'running',
      content: '已保存的部分回答',
      error: null,
      citations: [],
    },
  ]
  await openChat()
  cleanup()
  render(<App />)
  expect(await screen.findByText('已保存的部分回答')).toBeInTheDocument()
  await waitFor(() => expect(Stream.all.at(-1)?.closed).toBe(false))
  act(() => Stream.all.at(-1)!.onerror?.())
  expect(await screen.findByText(/正在重连/)).toBeInTheDocument()
  runs[0].content = '已保存的部分回答，加上后续步骤'
  act(() => Stream.all.at(-1)!.snapshot(runs[0]))
  expect(screen.getByText('已保存的部分回答，加上后续步骤')).toBeInTheDocument()
})
it('offers source links for a completed answer and refuses unsafe source URLs', async () => {
  messages = [
    { id: 1, role: 'user', content: '资料来源' },
    { id: 2, role: 'assistant', content: '参考如下' },
  ]
  runs = [
    {
      id: 1,
      conversation_id: 1,
      user_message_id: 1,
      assistant_message_id: 2,
      status: 'completed',
      content: '参考如下',
      error: null,
      citations: [
        {
          type: 'technique',
          id: 5,
          title: '打磨技巧',
          excerpt: '由粗到细',
          source_ids: [1, 2],
          sources: [
            { id: 1, title: '官方指南', url: 'https://example.com/guide' },
            { id: 2, title: '异常来源', url: 'javascript:alert(1)' },
          ],
        },
      ],
    },
  ]
  await openChat()
  expect(screen.getByRole('link', { name: /官方指南/ })).toHaveAttribute(
    'href',
    'https://example.com/guide',
  )
  expect(
    screen.queryByRole('link', { name: /异常来源/ }),
  ).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: /打磨技巧/ })).toBeInTheDocument()
})

it('persists renamed conversations, deletes them and can create another conversation', async () => {
  const user = await openChat()
  await user.click(screen.getByRole('button', { name: '重命名' }))
  await user.clear(screen.getByRole('textbox', { name: '会话名称' }))
  await user.type(
    screen.getByRole('textbox', { name: '会话名称' }),
    '扎古制作记录',
  )
  await user.click(screen.getByRole('button', { name: '保存名称' }))
  expect(
    await screen.findByRole('heading', { name: '扎古制作记录' }),
  ).toBeInTheDocument()
  cleanup()
  render(<App />)
  expect(
    await screen.findByRole('heading', { name: '扎古制作记录' }),
  ).toBeInTheDocument()
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  await user.click(screen.getByRole('button', { name: '删除会话' }))
  expect(
    await screen.findByRole('heading', { name: '开启你的制作对话' }),
  ).toBeInTheDocument()
  expect(
    screen.queryByRole('button', { name: '扎古制作记录' }),
  ).not.toBeInTheDocument()
  await user.click(screen.getAllByRole('button', { name: '新建对话' })[0])
  expect(
    await screen.findByRole('textbox', { name: '制作问题或补充要求' }),
  ).toBeEnabled()
  vi.restoreAllMocks()
})
it('preserves a new draft typed while the previous message is being submitted', async () => {
  const user = await openChat()
  const originalFetch = fetch
  let release: () => void = () => {}
  const gate = new Promise<void>((resolve) => {
    release = resolve
  })
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/conversations/1/runs')) await gate
      return originalFetch(input, init)
    }),
  )
  const input = screen.getByRole('textbox', { name: '制作问题或补充要求' })
  await user.type(input, '第一条问题')
  await user.click(screen.getByRole('button', { name: '发送' }))
  await user.clear(input)
  await user.type(input, '下一条补充')
  await act(async () => {
    release()
    await gate
  })
  expect(await screen.findByText('第一条问题')).toBeInTheDocument()
  expect(input).toHaveValue('下一条补充')
})
it('limits the editable message to the server accepted size', async () => {
  await openChat()
  expect(
    screen.getByRole('textbox', { name: '制作问题或补充要求' }),
  ).toHaveAttribute('maxlength', '16000')
})

it('follows new output at the bottom, preserves scrollback, and jumps to latest on demand', async () => {
  messages = [
    { id: 1, role: 'user', content: '持续输出' },
    { id: 2, role: 'assistant', content: '开头' },
  ]
  runs = [
    {
      id: 1,
      conversation_id: 1,
      user_message_id: 1,
      assistant_message_id: 2,
      status: 'running',
      content: '开头',
      error: null,
      citations: [],
    },
  ]
  const user = await openChat()
  const log = screen.getByLabelText('对话消息')
  Object.defineProperty(log, 'scrollHeight', {
    configurable: true,
    get: () => 1200,
  })
  Object.defineProperty(log, 'clientHeight', {
    configurable: true,
    get: () => 300,
  })
  act(() =>
    Stream.all.at(-1)!.snapshot({ ...runs[0], content: '开头和新步骤' }),
  )
  expect(log.scrollTop).toBe(1200)
  log.scrollTop = 200
  fireEvent.scroll(log)
  act(() => Stream.all.at(-1)!.snapshot({ ...runs[0], content: '更多新步骤' }))
  expect(log.scrollTop).toBe(200)
  await user.click(screen.getByRole('button', { name: '回到最新' }))
  expect(log.scrollTop).toBe(1200)
  expect(
    screen.queryByRole('button', { name: '回到最新' }),
  ).not.toBeInTheDocument()
})
it('sends with Ctrl or Command Enter without submitting during IME composition or plain Enter', async () => {
  const user = await openChat()
  const input = screen.getByRole('textbox', { name: '制作问题或补充要求' })
  await user.type(input, '快捷发送问题')
  fireEvent.keyDown(input, { key: 'Enter' })
  fireEvent.compositionStart(input)
  fireEvent.keyDown(input, { key: 'Enter', ctrlKey: true, isComposing: true })
  expect(
    screen.queryByText('快捷发送问题', { selector: '.chat-message-content' }),
  ).not.toBeInTheDocument()
  fireEvent.compositionEnd(input)
  fireEvent.keyDown(input, { key: 'Enter', ctrlKey: true })
  expect(
    await screen.findByText('快捷发送问题', {
      selector: '.chat-message-content',
    }),
  ).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: '停止生成' }))
  await user.type(input, 'Command 发送补充')
  fireEvent.keyDown(input, { key: 'Enter', metaKey: true })
  expect(
    await screen.findByText('Command 发送补充', {
      selector: '.chat-message-content',
    }),
  ).toBeInTheDocument()
})

it('filters only conversation titles and clears an empty search with the keyboard', async () => {
  conversationList = [
    conversation,
    { ...conversation, id: 2, title: 'Airbrush 喷涂记录' },
  ]
  messages = [{ id: 1, role: 'user', content: 'OnlyInMessage' }]
  const user = await openChat()
  const search = screen.getByRole('searchbox', { name: '搜索会话标题' })
  await user.type(search, 'airbrush')
  expect(
    screen.getByRole('button', { name: 'Airbrush 喷涂记录' }),
  ).toBeInTheDocument()
  expect(
    screen.queryByRole('button', { name: '我的制作计划' }),
  ).not.toBeInTheDocument()
  await user.clear(search)
  await user.type(search, 'OnlyInMessage')
  expect(screen.getByText('没有匹配的会话标题')).toBeInTheDocument()
  expect(
    screen.getByText('OnlyInMessage', { selector: '.chat-message-content' }),
  ).toBeInTheDocument()
  screen.getByRole('button', { name: '清除标题搜索' }).focus()
  await user.keyboard('{Enter}')
  expect(search).toHaveValue('')
  expect(
    screen.getByRole('button', { name: '我的制作计划' }),
  ).toBeInTheDocument()
  expect(
    screen.getByRole('button', { name: 'Airbrush 喷涂记录' }),
  ).toBeInTheDocument()
})

it('downloads current Markdown with partial output, status and citations in a safe filename', async () => {
  messages = [
    { id: 1, role: 'user', content: '如何打磨？' },
    { id: 2, role: 'assistant', content: '旧快照' },
  ]
  runs = [
    {
      id: 1,
      conversation_id: 1,
      user_message_id: 1,
      assistant_message_id: 2,
      status: 'stopped',
      content: '保留的部分回答\n```\n<script>bad</script>',
      error: '已手动停止',
      citations: [
        {
          type: 'technique',
          id: 5,
          title: '打磨技巧',
          excerpt: '由粗到细',
          source_ids: [1, 2],
          sources: [
            { id: 1, title: '官方指南', url: 'https://example.com/guide' },
            { id: 2, title: '异常来源', url: 'javascript:alert(1)' },
          ],
        },
      ],
    },
  ]
  const user = await openChat()
  await user.click(screen.getByRole('button', { name: '重命名' }))
  await user.clear(screen.getByRole('textbox', { name: '会话名称' }))
  await user.type(
    screen.getByRole('textbox', { name: '会话名称' }),
    '../模型:记录/测试',
  )
  await user.click(screen.getByRole('button', { name: '保存名称' }))
  await screen.findByRole('heading', { name: '../模型:记录/测试' })
  const blobs: Blob[] = []
  const create = vi.fn((blob: Blob) => {
    blobs.push(blob)
    return 'blob:test-download'
  })
  const revoke = vi.fn()
  Object.defineProperty(URL, 'createObjectURL', {
    configurable: true,
    value: create,
  })
  Object.defineProperty(URL, 'revokeObjectURL', {
    configurable: true,
    value: revoke,
  })
  let filename = ''
  const click = vi
    .spyOn(HTMLAnchorElement.prototype, 'click')
    .mockImplementation(function (this: HTMLAnchorElement) {
      filename = this.download
    })
  try {
    screen.getByRole('button', { name: '下载 Markdown' }).focus()
    await user.keyboard('{Enter}')
    expect(create).toHaveBeenCalledOnce()
    expect(filename).toMatch(/\.md$/)
    expect(filename).not.toMatch(/[\\/:*?"<>|\u0000-\u001f]/)
    const markdown = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(String(reader.result))
      reader.onerror = reject
      reader.readAsText(blobs[0])
    })
    expect(markdown).toContain('如何打磨？')
    expect(markdown).toContain('保留的部分回答')
    expect(markdown).not.toContain('旧快照')
    expect(markdown).toContain('已停止')
    expect(markdown).toContain('已手动停止')
    expect(markdown).toContain('technique:5')
    expect(markdown).toContain('由粗到细')
    expect(markdown).toContain('https://example.com/guide')
    expect(markdown).not.toContain('javascript:')
    expect(markdown).toContain('````text')
    expect(screen.getByRole('button', { name: '复制回答' })).toBeInTheDocument()
    await waitFor(() =>
      expect(revoke).toHaveBeenCalledWith('blob:test-download'),
    )
  } finally {
    click.mockRestore()
  }
})

it('disables Markdown download until the running answer is stopped', async () => {
  messages = [
    { id: 1, role: 'user', content: '继续制作' },
    { id: 2, role: 'assistant', content: '一半' },
  ]
  runs = [
    {
      id: 1,
      conversation_id: 1,
      user_message_id: 1,
      assistant_message_id: 2,
      status: 'running',
      content: '一半',
      error: null,
      citations: [],
    },
  ]
  const user = await openChat()
  expect(screen.getByRole('button', { name: '下载 Markdown' })).toBeDisabled()
  await user.click(screen.getByRole('button', { name: '停止生成' }))
  await waitFor(() =>
    expect(screen.getByRole('button', { name: '下载 Markdown' })).toBeEnabled(),
  )
})

it('restores canonical conversation deep links and reacts to history navigation', async () => {
  conversationList.push({
    id: 2,
    title: '第二份计划',
    created_at: '',
    updated_at: '',
  })
  const previous = vi.mocked(fetch).getMockImplementation()!
  vi.mocked(fetch).mockImplementation((input, init) =>
    String(input).endsWith('/conversations/2')
      ? reply({
          ...conversationList[1],
          messages: [
            {
              id: 20,
              conversation_id: 2,
              role: 'user',
              content: '第二份会话正文',
            },
          ],
          runs: [],
        })
      : previous(input, init),
  )
  window.history.replaceState(null, '', '/#/chat/2')
  render(<App />)
  expect(await screen.findByText('第二份会话正文')).toBeInTheDocument()
  window.location.hash = '/chat/1'
  await waitFor(() =>
    expect(screen.queryByText('第二份会话正文')).not.toBeInTheDocument(),
  )
  await waitFor(() =>
    expect(
      screen.getByRole('textbox', { name: '制作问题或补充要求' }),
    ).toBeEnabled(),
  )
})

it('shows only bounded retrieved source images and treats model image links as text', async () => {
  messages = [
    { id: 1, role: 'user', content: '参考图' },
    {
      id: 2,
      role: 'assistant',
      content: '![生成图](https://untrusted.example/image.png)',
    },
  ]
  runs = [
    {
      id: 1,
      conversation_id: 1,
      user_message_id: 1,
      assistant_message_id: 2,
      status: 'completed',
      content: messages[1].content,
      error: null,
      citations: [
        {
          type: 'technique',
          id: 5,
          title: '打磨技巧',
          excerpt: '由粗到细',
          source_ids: [],
          sources: [],
          attachment_ids: [8, 8, -1, 0, 9, 10, 11, 12, 13, 14],
        },
      ],
    },
  ]
  await openChat()
  const images = await screen.findAllByRole('img')
  expect(images).toHaveLength(6)
  expect(images.map((image) => image.getAttribute('src'))).toEqual(
    [8, 9, 10, 11, 12, 13].map((id) => `/api/v1/attachments/${id}`),
  )
  expect(screen.getByText('检索到的原始图片')).toBeInTheDocument()
  expect(screen.queryByRole('link', { name: '生成图' })).not.toBeInTheDocument()
})
