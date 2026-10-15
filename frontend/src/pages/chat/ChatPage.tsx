import AgentTrace, { AgentLiveProgress } from '../../components/AgentTrace'
import RetrievedImages from '../../components/RetrievedImages'
import { useLocation, useNavigate } from 'react-router'
import {
  Copy,
  Download,
  MessageSquare,
  Plus,
  Send,
  Square,
  WandSparkles,
} from 'lucide-react'
import { useEffect, useRef, useState, type FormEvent } from 'react'
import '../../chat.css'
import {
  chatApi,
  isActive,
  type AgentConfig,
  type AgentProgress,
  type Citation,
  type Conversation,
  type ConversationDetail,
  type Run,
} from '../../chatApi'
const statusLabels = {
  queued: '等待生成',
  running: '正在生成',
  completed: '已完成',
  stopped: '已停止',
  failed: '生成失败',
}
const errorText = (error: unknown) =>
  error instanceof Error ? error.message : '操作失败，请重试'
const safeUrl = (url: string | null) =>
  url && /^https?:\/\//i.test(url) ? url : null

export default function ChatWorkbench({
  onOpenReference,
}: {
  onOpenReference: (citation: Citation) => void
}) {
  const [config, setConfig] = useState<AgentConfig | null>(null)
  const [progress, setProgress] = useState<{
    runId: number
    value: AgentProgress
  } | null>(null)
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [titleSearch, setTitleSearch] = useState('')
  const visibleConversations = conversations.filter((item) =>
    item.title
      .toLocaleLowerCase()
      .includes(titleSearch.trim().toLocaleLowerCase()),
  )
  const [detail, setDetail] = useState<ConversationDetail | null>(null)
  const location = useLocation()
  const navigate = useNavigate()
  const routeId = Number(location.pathname.match(/^\/chat\/(\d+)$/)?.[1])
  const selected =
    conversations.find((item) => item.id === routeId)?.id ??
    conversations[0]?.id ??
    null
  const setSelected = (id: number | null) => {
    void navigate(id === null ? '/chat' : `/chat/${id}`)
  }
  const selectedRef = useRef(selected)
  selectedRef.current = selected
  const [draft, setDraft] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [guide, setGuide] = useState(false)
  const [rename, setRename] = useState<string | null>(null)
  const [connection, setConnection] = useState('')
  const messageViewport = useRef<HTMLDivElement>(null)
  const followingLatest = useRef(true)
  const composing = useRef(false)
  const [showLatest, setShowLatest] = useState(false)
  const pendingRequest = useRef<{ payload: string; id: string } | null>(null)
  const latest = detail?.runs.at(-1)
  const active = isActive(latest)

  useEffect(() => {
    let live = true
    Promise.all([chatApi.config(), chatApi.list()])
      .then(([settings, list]) => {
        if (live) {
          setConfig(settings)
          setConversations(list)
        }
      })
      .catch((err) => {
        if (live) setError(errorText(err))
      })
      .finally(() => {
        if (live) setLoading(false)
      })
    return () => {
      live = false
    }
  }, [])

  useEffect(() => {
    let live = true
    followingLatest.current = true
    setShowLatest(false)
    setDetail(null)
    setRename(null)
    setError('')
    setNotice('')
    setDraft('')
    if (selected !== null)
      chatApi
        .detail(selected)
        .then((data) => {
          if (live) setDetail(data)
        })
        .catch((err) => {
          if (live) setError(errorText(err))
        })
    return () => {
      live = false
    }
  }, [selected])

  useEffect(() => {
    if (followingLatest.current && messageViewport.current)
      messageViewport.current.scrollTop = messageViewport.current.scrollHeight
  }, [detail?.id, detail?.messages.length, latest?.content, latest?.status])

  function jumpToLatest() {
    followingLatest.current = true
    setShowLatest(false)
    if (messageViewport.current)
      messageViewport.current.scrollTop = messageViewport.current.scrollHeight
  }

  function applyRun(run: Run) {
    setDetail((current) =>
      !current || current.id !== run.conversation_id
        ? current
        : {
            ...current,
            runs: current.runs.some((item) => item.id === run.id)
              ? current.runs.map((item) => (item.id === run.id ? run : item))
              : [...current.runs, run],
            messages: current.messages.map((message) =>
              message.id === run.assistant_message_id
                ? { ...message, content: run.content }
                : message,
            ),
          },
    )
  }

  useEffect(() => {
    if (!latest || !isActive(latest)) {
      setConnection('')
      return
    }
    let live = true
    const stream = new EventSource(`/api/v1/runs/${latest.id}/events`)
    setConnection('正在连接…')
    stream.onopen = () => {
      if (live) setConnection('实时连接')
    }
    stream.addEventListener('progress', (event) => {
      if (!live) return
      try {
        const value = JSON.parse((event as MessageEvent).data) as AgentProgress
        setProgress({ runId: latest.id, value })
      } catch {
        // Progress is advisory; the next snapshot still updates the answer.
      }
    })
    stream.addEventListener('snapshot', (event) => {
      if (!live) return
      try {
        const run = JSON.parse((event as MessageEvent).data) as Run
        if (run.id !== latest.id || run.conversation_id !== selected) return
        applyRun(run)
        setConnection(isActive(run) ? '实时连接' : '')
        if (!isActive(run)) stream.close()
      } catch {
        setConnection('响应格式异常，正在重新同步…')
      }
    })
    stream.onerror = () => {
      if (!live) return
      setConnection('连接中断，正在重连…')
      void chatApi
        .run(latest.id)
        .then((run) => {
          if (live) {
            applyRun(run)
            if (!isActive(run)) stream.close()
          }
        })
        .catch(() => {})
    }
    return () => {
      live = false
      stream.close()
    }
  }, [latest?.id, active, selected])

  async function action(task: () => Promise<void>) {
    if (busy) return
    setBusy(true)
    setError('')
    setNotice('')
    try {
      await task()
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }
  async function refreshList() {
    setConversations(await chatApi.list())
  }
  async function createConversation() {
    await action(async () => {
      const created = await chatApi.create()
      await refreshList()
      setTitleSearch('')
      setSelected(created.id)
    })
  }
  async function submit(kind: 'send' | 'retry') {
    if (!detail || !config?.configured || (kind === 'send' && !draft.trim()))
      return
    const id = detail.id
    const content = draft.trim()
    if (kind === 'send' && content.length > 16000) {
      setError('消息最多 16000 字，请缩短后发送。')
      return
    }
    const payload = JSON.stringify([id, kind, latest?.id, content])
    if (pendingRequest.current?.payload !== payload)
      pendingRequest.current = { payload, id: crypto.randomUUID() }
    const requestId = pendingRequest.current.id
    await action(async () => {
      await (kind === 'retry' && latest
        ? await chatApi.retry(latest.id, requestId)
        : latest && (active || latest.status === 'stopped')
          ? await chatApi.steer(latest.id, content, requestId)
          : await chatApi.start(id, content, requestId))
      const updated = await chatApi.detail(id)
      if (selectedRef.current === id) {
        setDetail(updated)
        if (kind === 'send')
          setDraft((current) => (current === draft ? '' : current))
      }
      pendingRequest.current = null
      await refreshList()
    })
  }

  function downloadConversation() {
    if (!detail || busy || detail.runs.some(isActive)) return
    let url: string | undefined
    try {
      url = URL.createObjectURL(
        new Blob([conversationMarkdown(detail)], {
          type: 'text/markdown;charset=utf-8',
        }),
      )
      const link = document.createElement('a')
      link.href = url
      link.download = conversationFilename(detail)
      document.body.append(link)
      try {
        link.click()
      } finally {
        link.remove()
      }
      setNotice('已准备 Markdown 下载')
    } catch {
      setError('无法下载会话，请重试。')
    } finally {
      if (url) {
        const downloadUrl = url
        window.setTimeout(() => URL.revokeObjectURL(downloadUrl), 0)
      }
    }
  }

  return (
    <section className="chat-page" aria-label="对话工作台">
      <div className="page-intro">
        <div>
          <span className="page-kicker">制作搭档 / CHAT</span>
          <h1>从一个想法，做到下一步。</h1>
          <p>说说你的模型和目标，一起梳理材料、步骤与疑问。</p>
        </div>
        <MessageSquare
          className="chat-intro-icon"
          size={42}
          strokeWidth={1.2}
        />
      </div>
      {error && (
        <div className="alert error" role="alert">
          {error}
          <button
            className="ghost-button"
            onClick={() => window.location.reload()}
          >
            刷新重连
          </button>
        </div>
      )}
      {notice && (
        <div className="alert success" role="status">
          {notice}
        </div>
      )}
      {config && !config.configured && (
        <div className="chat-config" role="status">
          <strong>模型服务尚未就绪</strong>
          <p>{config.message}</p>
          <small>
            配置服务后刷新页面即可开始。你可以先整理需求并保存会话。
          </small>
        </div>
      )}
      <div className="chat-layout">
        <aside className="chat-history" aria-label="会话列表">
          <button
            className="outline-button"
            disabled={busy || loading}
            onClick={() => void createConversation()}
          >
            <Plus size={16} />
            新建对话
          </button>
          <div className="chat-title-search">
            <label htmlFor="chat-title-search">搜索会话标题</label>
            <input
              id="chat-title-search"
              type="search"
              value={titleSearch}
              placeholder="按标题筛选"
              onChange={(event) => setTitleSearch(event.target.value)}
            />
            {titleSearch && (
              <button
                className="ghost-button"
                onClick={() => setTitleSearch('')}
              >
                清除标题搜索
              </button>
            )}
          </div>
          <span className="chat-history-label">
            制作记录 ·{' '}
            {titleSearch.trim()
              ? `${visibleConversations.length} / ${conversations.length}`
              : conversations.length}
          </span>
          <div className="chat-conversation-list">
            {loading ? (
              <p role="status">正在读取会话…</p>
            ) : conversations.length === 0 ? (
              <p className="chat-muted">还没有对话。新建一份制作记录吧。</p>
            ) : visibleConversations.length === 0 ? (
              <p className="chat-muted" role="status">
                没有匹配的会话标题
              </p>
            ) : (
              visibleConversations.map((item) => (
                <button
                  key={item.id}
                  disabled={busy}
                  className={`conversation-button ${selected === item.id ? 'selected' : ''}`}
                  aria-pressed={selected === item.id}
                  onClick={() => setSelected(item.id)}
                >
                  <MessageSquare size={15} />
                  <span>{item.title}</span>
                </button>
              ))
            )}
          </div>
        </aside>
        <div className="chat-main">
          {detail ? (
            <>
              <header className="chat-heading">
                <div>
                  <h2>{detail.title}</h2>
                  <span className="chat-muted">
                    {config?.model ? `模型：${config.model}` : '制作对话'}
                    {connection && ` · ${connection}`}
                  </span>
                </div>
                <div className="chat-heading-actions">
                  <button
                    className="ghost-button"
                    disabled={busy || detail.runs.some(isActive)}
                    title={
                      active
                        ? '停止生成后可下载完整会话快照'
                        : '下载当前会话、生成状态和参考资料'
                    }
                    onClick={downloadConversation}
                  >
                    <Download size={14} />
                    下载 Markdown
                  </button>
                  <button
                    className="ghost-button"
                    disabled={busy}
                    onClick={() => setRename(detail.title)}
                  >
                    重命名
                  </button>
                  <button
                    className="ghost-button"
                    disabled={busy || active}
                    onClick={() => {
                      if (
                        window.confirm(
                          '删除这个会话及全部消息？此操作无法撤销。',
                        )
                      )
                        void action(async () => {
                          await chatApi.delete(detail.id)
                          const list = await chatApi.list()
                          setConversations(list)
                          setSelected(list[0]?.id ?? null)
                        })
                    }}
                  >
                    删除会话
                  </button>
                </div>
              </header>
              {rename !== null && (
                <form
                  className="chat-rename"
                  onSubmit={(event) => {
                    event.preventDefault()
                    if (rename.trim())
                      void action(async () => {
                        const updated = await chatApi.rename(
                          detail.id,
                          rename.trim(),
                        )
                        setDetail((current) =>
                          current
                            ? { ...current, title: updated.title }
                            : current,
                        )
                        setRename(null)
                        await refreshList()
                      })
                  }}
                >
                  <label>
                    会话名称
                    <input
                      value={rename}
                      maxLength={120}
                      onChange={(event) => setRename(event.target.value)}
                      autoFocus
                    />
                  </label>
                  <button
                    className="outline-button"
                    disabled={busy || !rename.trim()}
                  >
                    保存名称
                  </button>
                  <button
                    type="button"
                    className="ghost-button"
                    onClick={() => setRename(null)}
                  >
                    取消
                  </button>
                </form>
              )}
              <div
                className="chat-messages"
                aria-label="对话消息"
                ref={messageViewport}
                onScroll={(event) => {
                  const view = event.currentTarget
                  const nearBottom =
                    view.scrollHeight - view.scrollTop - view.clientHeight < 80
                  followingLatest.current = nearBottom
                  setShowLatest(!nearBottom)
                }}
              >
                {detail.messages.length === 0 && (
                  <div className="chat-welcome">
                    <WandSparkles size={30} strokeWidth={1.4} />
                    <h3>这次想做什么模型？</h3>
                    <p>可以直接提问，也可以用需求引导整理一份制作说明。</p>
                    <div className="chat-suggestions">
                      {[
                        '第一次素组，需要准备哪些工具？',
                        '如何减少零件上的水口痕迹？',
                      ].map((prompt) => (
                        <button
                          key={prompt}
                          className="outline-button"
                          onClick={() => setDraft(prompt)}
                        >
                          {prompt}
                        </button>
                      ))}
                    </div>
                  </div>
                )}
                {detail.messages.map((message) => {
                  const run = detail.runs.find(
                    (item) => item.assistant_message_id === message.id,
                  )
                  return (
                    <article
                      className={`chat-message ${message.role}`}
                      key={message.id}
                      aria-label={
                        message.role === 'user' ? '我的消息' : '助手回答'
                      }
                    >
                      <div className="chat-message-meta">
                        <strong>
                          {message.role === 'user' ? '你' : '制作助手'}
                        </strong>
                        {run && (
                          <span
                            className={`run-status ${run.status}`}
                            role="status"
                          >
                            {statusLabels[run.status]}
                          </span>
                        )}
                      </div>
                      <div className="chat-message-content">
                        {run?.content ||
                          message.content ||
                          (run && isActive(run)
                            ? '正在思考制作步骤…'
                            : '本次未生成回答。')}
                      </div>
                      {run?.error && (
                        <p className="chat-run-error">{run.error}</p>
                      )}
                      {run && isActive(run) && progress?.runId === run.id && (
                        <AgentLiveProgress progress={progress.value} />
                      )}
                      {run && config?.mode === 'agent' && !isActive(run) && (
                        <AgentTrace runId={run.id} />
                      )}
                      {run && !!run.citations.length && (
                        <div className="chat-citations">
                          <p>本轮检索到的参考资料 · 请结合原文核对</p>
                          {run.citations.map((citation) => (
                            <div
                              className="citation-card"
                              key={`${citation.type}-${citation.id}`}
                            >
                              <button
                                className="citation-title"
                                onClick={() => onOpenReference(citation)}
                              >
                                {citation.title}
                                <span>查看知识库 ↗</span>
                              </button>
                              <p>{citation.excerpt}</p>
                              <RetrievedImages citation={citation} />
                              {citation.sources.map((source) =>
                                safeUrl(source.url) ? (
                                  <a
                                    key={source.id}
                                    href={safeUrl(source.url)!}
                                    target="_blank"
                                    rel="noreferrer"
                                  >
                                    {source.title} ↗
                                  </a>
                                ) : (
                                  <span className="chat-muted" key={source.id}>
                                    {source.title}（本地来源）
                                  </span>
                                ),
                              )}
                            </div>
                          ))}
                        </div>
                      )}
                      {message.role === 'assistant' &&
                        (run?.content || message.content) && (
                          <button
                            className="ghost-button chat-copy"
                            onClick={() =>
                              void action(async () => {
                                await navigator.clipboard.writeText(
                                  run?.content || message.content,
                                )
                                setNotice('回答已复制')
                              })
                            }
                          >
                            <Copy size={13} />
                            复制回答
                          </button>
                        )}
                    </article>
                  )
                })}
              </div>
              {showLatest && (
                <button
                  className="outline-button chat-jump"
                  onClick={jumpToLatest}
                >
                  回到最新
                </button>
              )}
              <div className="chat-composer">
                <div className="chat-tools">
                  <button
                    className="ghost-button"
                    aria-expanded={guide}
                    onClick={() => setGuide(!guide)}
                  >
                    <WandSparkles size={15} />
                    整理制作需求
                  </button>
                  {latest && !active && (
                    <button
                      className="ghost-button"
                      disabled={busy || !config?.configured}
                      onClick={() => void submit('retry')}
                    >
                      {latest.status === 'failed' ? '重试回答' : '重新生成'}
                    </button>
                  )}
                  {active && (
                    <button
                      className="outline-button"
                      disabled={busy}
                      onClick={() =>
                        void action(async () => {
                          if (latest) applyRun(await chatApi.stop(latest.id))
                        })
                      }
                    >
                      <Square size={13} />
                      停止生成
                    </button>
                  )}
                </div>
                {guide && (
                  <GuidedDraft
                    onDraft={(text) => {
                      setDraft(text)
                      setGuide(false)
                    }}
                  />
                )}
                <form
                  onSubmit={(event) => {
                    event.preventDefault()
                    void submit('send')
                  }}
                >
                  <label className="sr-only" htmlFor="chat-draft">
                    制作问题或补充要求
                  </label>
                  <textarea
                    aria-keyshortcuts="Control+Enter Meta+Enter"
                    onCompositionStart={() => {
                      composing.current = true
                    }}
                    onCompositionEnd={() => {
                      composing.current = false
                    }}
                    onKeyDown={(event) => {
                      if (
                        event.key === 'Enter' &&
                        (event.ctrlKey || event.metaKey) &&
                        !event.nativeEvent.isComposing &&
                        !composing.current &&
                        event.keyCode !== 229
                      ) {
                        event.preventDefault()
                        if (!busy) void submit('send')
                      }
                    }}
                    id="chat-draft"
                    value={draft}
                    onChange={(event) => setDraft(event.target.value)}
                    placeholder={
                      active
                        ? '有新的要求？输入后打断并发送…'
                        : '描述你的模型、遇到的问题，或想达到的效果…'
                    }
                    rows={3}
                    maxLength={16000}
                  />
                  <div className="chat-send-row">
                    <small>
                      {active
                        ? '补充要求会停止当前生成，并保留已有回答。'
                        : '回答由模型生成，可通过参考资料核对。'}
                      <br />
                      Ctrl / ⌘ + Enter 发送 · Enter 换行
                    </small>
                    <button
                      className="primary-button"
                      disabled={busy || !config?.configured || !draft.trim()}
                    >
                      <Send size={16} />
                      {active ? '打断并发送' : '发送'}
                    </button>
                  </div>
                </form>
              </div>
            </>
          ) : (
            <div className="chat-welcome">
              <MessageSquare size={36} />
              <h2>{selected ? '正在读取对话…' : '开启你的制作对话'}</h2>
              <p>每个会话保存一个项目的想法、步骤与后续调整。</p>
              {!selected && (
                <button
                  className="primary-button"
                  disabled={busy || loading}
                  onClick={() => void createConversation()}
                >
                  新建对话
                </button>
              )}
            </div>
          )}
        </div>
      </div>
    </section>
  )
}
function GuidedDraft({ onDraft }: { onDraft: (text: string) => void }) {
  const fields = [
    '模型类型',
    '制作目标',
    '经验水平',
    '时间与预算',
    '已有工具和材料',
  ]
  const [values, setValues] = useState<Record<string, string>>({})
  function submit(event: FormEvent) {
    event.preventDefault()
    onDraft(
      `请帮我规划制作步骤，有缺失的信息请先问我。\n${fields.map((field) => `${field}：${values[field]?.trim() || '待补充'}`).join('\n')}`,
    )
  }
  return (
    <form className="chat-guide" onSubmit={submit}>
      <p>填写已知信息，生成后仍可自由编辑。</p>
      <div>
        {fields.map((field) => (
          <label key={field}>
            {field}
            <input
              value={values[field] || ''}
              onChange={(event) =>
                setValues({ ...values, [field]: event.target.value })
              }
            />
          </label>
        ))}
      </div>
      <button className="outline-button">填入草稿</button>
    </form>
  )
}

function markdownText(value: string) {
  return value
    .replace(/[\\`*_{}[\]()#+.!<>|~]/g, '\\$&')
    .replace(/[\r\n]+/g, ' ')
}
function textBlock(value: string) {
  const longest = Math.max(
    2,
    ...(value.match(/`+/g) || []).map((run) => run.length),
  )
  const fence = '`'.repeat(longest + 1)
  return `${fence}text\n${value}\n${fence}`
}
function conversationFilename(conversation: Conversation) {
  const title = conversation.title
    .normalize('NFC')
    .replace(
      /[\\/:*?"<>|\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/g,
      '-',
    )
    .replace(/^[.\s]+|[.\s]+$/g, '')
    .slice(0, 80)
  return `会话-${conversation.id}-${title || '制作记录'}.md`
}
function conversationMarkdown(detail: ConversationDetail) {
  const lines = [
    `# ${markdownText(detail.title)}`,
    '',
    `会话编号：${detail.id}`,
    '',
  ]
  if (detail.created_at)
    lines.push(`创建时间：${markdownText(detail.created_at)}`, '')
  if (!detail.messages.length) lines.push('此会话暂无消息。', '')
  for (const message of detail.messages) {
    const run = detail.runs.find(
      (item) => item.assistant_message_id === message.id,
    )
    lines.push(`## ${message.role === 'user' ? '你' : '制作助手'}`, '')
    if (run) lines.push(`生成状态：${statusLabels[run.status]}`, '')
    lines.push(textBlock(run ? run.content : message.content), '')
    if (run?.error) lines.push('生成说明：', '', textBlock(run.error), '')
    if (run?.citations.length) {
      lines.push(
        '### 本轮检索参考资料',
        '',
        '请结合原文核对；检索结果不代表回答已经核实。',
        '',
      )
      for (const citation of run.citations) {
        lines.push(
          `#### ${markdownText(citation.title)} [${citation.type}:${citation.id}]`,
          '',
          textBlock(citation.excerpt),
          '',
        )
        for (const source of citation.sources) {
          const url = safeUrl(source.url)
          lines.push(
            `- ${markdownText(source.title)}${url ? `：<${url.replace(/[<>\s]/g, (char) => encodeURIComponent(char))}>` : '（本地来源或不可用链接）'}`,
          )
        }
        lines.push('')
      }
    }
  }
  return lines.join('\n')
}
