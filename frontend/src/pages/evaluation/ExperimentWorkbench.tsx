import { useEffect, useRef, useState, type FormEvent } from 'react'
import { evaluationApi, type Dataset } from '../../evaluationApi'
import { Link } from 'react-router'
import DemoDatasetToggle, { firstRealDatasetId } from './DemoDatasetToggle'
import {
  experimentApi,
  type Experiment,
  type ExperimentInput,
  type IntegrationStatus,
} from '../../experimentApi'
import '../../experiments.css'

const active = (job: Experiment) =>
  job.status === 'queued' || job.status === 'running'
const statuses: Record<Experiment['status'], string> = {
  queued: '排队中',
  running: '运行中',
  completed: '已完成',
  failed: '失败',
  stopped: '已停止',
}
const message = (e: unknown) =>
  e instanceof Error ? e.message : '请求失败，请重试'
const metricLabels: Record<string, string> = {
  hit_rate: '命中率',
  recall: 'Recall',
  mrr: 'MRR',
  ndcg: 'nDCG',
  no_answer_accuracy: '无答案题正确留空',
  positive_cases: '正例题数',
  no_answer_cases: '无答案题数',
  answer_nonempty: '回答非空',
  citation_validity: '引用格式与编号有效',
  uncertainty_marker: '不确定性措辞标记',
}
function safeLink(url: string) {
  try {
    return new URL(url).protocol === 'https:'
  } catch {
    return false
  }
}
function ExperimentView({ job }: { job: Experiment }) {
  return (
    <section className="experiment-report" aria-label="实验详情">
      <h3>
        实验 #{job.id} · {job.dataset_name}
      </h3>
      <p>
        <strong>{statuses[job.status]}</strong> ·{' '}
        {job.mode === 'answers' ? '回答对比' : '检索对比'} ·{' '}
        {job.corpus === 'demo' ? '合成示例' : '本机真实资料'} · {job.created_at}
      </p>
      <label className="experiment-progress">
        已处理 {job.progress} / {job.total}
        <progress
          aria-label="实验进度"
          max={Math.max(1, job.total)}
          value={job.progress}
        />
      </label>
      {job.error && (
        <p role="alert" className="alert error">
          {job.error}
        </p>
      )}
      {(job.status === 'stopped' || job.status === 'failed') && (
        <p>结果可能不完整；已经上传的部分记录仍可能保留在 LangSmith。</p>
      )}
      <div className="experiment-links">
        {job.links
          .filter((link) => safeLink(link.url))
          .map((link, i) => (
            <a
              key={`${link.url}-${i}`}
              href={link.url}
              target="_blank"
              rel="noreferrer"
            >
              {link.label} <span aria-hidden="true">↗</span>
            </a>
          ))}
      </div>
      {!job.results?.length && (
        <p className="evaluation-muted">尚无逐题结果。</p>
      )}
      {job.results?.map((result, i) => (
        <details key={`${result.variant}-${result.case_id}-${i}`}>
          <summary>
            {result.query.trim() || '空查询'} <small> · {result.variant}</small>
          </summary>
          {result.error && <p role="alert">{result.error}</p>}
          {result.answer !== null && (
            <p className="experiment-answer">{result.answer || '空回答'}</p>
          )}
          <p>检索命中：{result.hit_keys.join('、') || '无'}</p>
          <dl className="experiment-metrics">
            {Object.entries(result.metrics).map(([key, value]) => (
              <div key={key}>
                <dt>{metricLabels[key] || key}</dt>
                <dd>
                  {value === null
                    ? '不适用'
                    : Number.isFinite(value)
                      ? value.toFixed(3)
                      : '未知'}
                </dd>
              </div>
            ))}
          </dl>
          <p>
            耗时 {Math.round(result.latency_ms)} ms · Token{' '}
            {result.usage
              ? `${result.usage.input_tokens} 输入 / ${result.usage.output_tokens} 输出`
              : '未知'}{' '}
            · 费用估算{' '}
            {result.cost_usd === null
              ? '未知'
              : `$${result.cost_usd.toFixed(6)} USD`}
          </p>
        </details>
      ))}
    </section>
  )
}

export default function ExperimentWorkbench() {
  const [status, setStatus] = useState<IntegrationStatus | null>(null),
    [datasets, setDatasets] = useState<Dataset[]>([]),
    [history, setHistory] = useState<Experiment[]>([])
  const [showDemo, setShowDemo] = useState(false)
  const [datasetId, setDatasetId] = useState(''),
    [mode, setMode] = useState<'retrieval' | 'answers'>('retrieval'),
    [models, setModels] = useState<string[]>([]),
    [limit, setLimit] = useState(3),
    [k, setK] = useState(5)
  const [job, setJob] = useState<Experiment | null>(null),
    [activeId, setActiveId] = useState<number | null>(null),
    [loading, setLoading] = useState(true),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(''),
    [reload, setReload] = useState(0)
  const mounted = useRef(false),
    viewRequest = useRef(0),
    mutation = useRef(0),
    request = useRef<{ signature: string; id: string } | null>(null)
  useEffect(() => {
    mounted.current = true
    let live = true
    const controller = new AbortController()
    setLoading(true)
    setError('')
    Promise.all([
      experimentApi.status(controller.signal),
      evaluationApi.datasets(),
      experimentApi.history(controller.signal),
    ])
      .then(([s, d, h]) => {
        if (!live) return
        setStatus(s)
        setDatasets(d)
        setHistory(h)
        setDatasetId(firstRealDatasetId(d))
        setModels(s.model ? [s.model] : [])
        const running = h.find(active)
        setActiveId(running?.id ?? null)
        if (running) setJob(running)
      })
      .catch((e) => {
        if (live) setError(message(e))
      })
      .finally(() => {
        if (live) setLoading(false)
      })
    return () => {
      live = false
      mounted.current = false
      controller.abort()
      viewRequest.current++
      mutation.current++
    }
  }, [reload])
  function merge(next: Experiment) {
    setHistory((items) =>
      [next, ...items.filter((item) => item.id !== next.id)]
        .sort((a, b) => b.id - a.id)
        .slice(0, 50),
    )
  }
  useEffect(() => {
    if (activeId === null) return
    let live = true
    let timer: ReturnType<typeof setTimeout>
    const controller = new AbortController()
    async function poll() {
      const version = mutation.current
      try {
        const next = await experimentApi.get(activeId!, controller.signal)
        if (!live) return
        if (version === mutation.current) {
          merge(next)
          setJob((current) => (current?.id === next.id ? next : current))
          if (!active(next)) {
            setActiveId(null)
            return
          }
        }
      } catch (e) {
        if (live && version === mutation.current)
          setError(`进度更新失败：${message(e)}`)
      }
      if (live) timer = setTimeout(() => void poll(), 1500)
    }
    void poll()
    return () => {
      live = false
      controller.abort()
      clearTimeout(timer)
    }
  }, [activeId])
  async function show(id: number) {
    const token = ++viewRequest.current
    setError('')
    try {
      const next = await experimentApi.get(id)
      if (mounted.current && token === viewRequest.current) {
        setJob(next)
        merge(next)
      }
    } catch (e) {
      if (mounted.current && token === viewRequest.current) setError(message(e))
    }
  }
  async function start(e: FormEvent) {
    e.preventDefault()
    if (!canStart) return
    ++viewRequest.current
    ++mutation.current
    setBusy(true)
    setError('')
    const payload = {
      dataset_id: datasetId,
      mode,
      models: mode === 'answers' ? models : [],
      limit,
      k,
    }
    const signature = JSON.stringify(payload)
    if (request.current?.signature !== signature)
      request.current = { signature, id: crypto.randomUUID() }
    try {
      const next = await experimentApi.start({
        ...payload,
        request_id: request.current!.id,
      } satisfies ExperimentInput)
      if (!mounted.current) return
      request.current = null
      setJob(next)
      merge(next)
      setActiveId(active(next) ? next.id : null)
    } catch (e) {
      if (mounted.current) setError(message(e))
    } finally {
      if (mounted.current) setBusy(false)
    }
  }
  async function stop() {
    if (activeId === null || busy) return
    ++mutation.current
    ++viewRequest.current
    setBusy(true)
    setError('')
    try {
      const next = await experimentApi.stop(activeId)
      if (mounted.current) {
        setJob(next)
        merge(next)
        setActiveId(active(next) ? next.id : null)
      }
    } catch (e) {
      if (mounted.current) setError(message(e))
    } finally {
      if (mounted.current) setBusy(false)
    }
  }
  const current = datasets.find((d) => d.id === datasetId)
  const availableModels = [
    ...new Set(
      [status?.model, ...(status?.compare_models || [])].filter(
        (m): m is string => !!m,
      ),
    ),
  ].slice(0, 2)
  const canStart =
    !loading &&
    !busy &&
    activeId === null &&
    !!status?.langsmith.configured &&
    !!current?.case_count &&
    (current.corpus !== 'demo' || showDemo) &&
    (mode === 'retrieval' || (!!status.model_configured && models.length > 0))
  return (
    <section className="experiment-workbench">
      <h2>LangSmith 实验</h2>
      <p>将检索或回答对比保存到 LangSmith，逐题查看进度与历史结果。</p>
      {error && (
        <p className="alert error" role="alert">
          {error}
        </p>
      )}
      {loading ? (
        <p role="status">正在读取实验配置…</p>
      ) : (
        <div className="experiment-status">
          <p>
            <strong>
              {status?.langsmith.configured
                ? 'LangSmith 已配置'
                : 'LangSmith 未配置'}
            </strong>{' '}
            · 配置状态不代表连接已验证。
          </p>
          {status && (
            <>
              <p>{status.langsmith.message}</p>
              <p>
                项目：{status.langsmith.project} · 普通聊天追踪：
                {status.langsmith.tracing_enabled ? '开启' : '关闭'} · 模型：
                {status.model || '未配置'}
              </p>
            </>
          )}
          {!status?.langsmith.configured && (
            <p>
              请在后端 .env 配置 LANGSMITH_API_KEY，可选
              LANGSMITH_PROJECT、LANGSMITH_ENDPOINT；保存并按部署方式重启后刷新配置。
            </p>
          )}
          {!status?.model_configured && (
            <p>
              回答实验需要在后端 .env 配置 AI_API_KEY、AI_MODEL；可用
              AI_COMPARE_MODEL 增加对比模型。
            </p>
          )}
          <button
            disabled={busy || activeId !== null}
            onClick={() => setReload((n) => n + 1)}
          >
            刷新配置
          </button>
        </div>
      )}
      <form onSubmit={start}>
        <fieldset
          disabled={loading || busy || activeId !== null}
          className="experiment-controls"
        >
          <legend>实验设置</legend>
          <label>
            实验测评集
            <select
              value={datasetId}
              onChange={(e) => setDatasetId(e.target.value)}
            >
              <option value="">请选择测评集</option>
              {datasets
                .filter((d) => showDemo || d.corpus === 'live')
                .map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name}（
                    {d.corpus === 'demo' ? '合成示例' : '本机真实资料'}，
                    {d.case_count} 题）
                  </option>
                ))}
            </select>
          </label>
          <DemoDatasetToggle
            checked={showDemo}
            onChange={(enabled) => {
              setShowDemo(enabled)
              if (!enabled && current?.corpus === 'demo')
                setDatasetId(firstRealDatasetId(datasets))
            }}
          />
          {!loading && !firstRealDatasetId(datasets) && (
            <p className="evaluation-note">
              暂无真实资料测评集。
              <Link to="/evaluation/datasets">新建真实资料测评集</Link>
            </p>
          )}
          <label>
            实验模式
            <select
              value={mode}
              onChange={(e) => setMode(e.target.value as typeof mode)}
            >
              <option value="retrieval">检索对比 · 基线 / 本地排序</option>
              <option value="answers">回答对比 · 模型生成</option>
            </select>
          </label>
          <label>
            抽样题数
            <select
              value={limit}
              onChange={(e) => setLimit(Number(e.target.value))}
            >
              {Array.from({ length: 10 }, (_, i) => (
                <option key={i + 1}>{i + 1}</option>
              ))}
            </select>
          </label>
          <label>
            实验返回条数 k
            <select value={k} onChange={(e) => setK(Number(e.target.value))}>
              {Array.from({ length: 20 }, (_, i) => (
                <option key={i + 1}>{i + 1}</option>
              ))}
            </select>
          </label>
          {mode === 'answers' && (
            <fieldset className="experiment-models">
              <legend>对比模型（最多 2 个）</legend>
              {availableModels.map((model) => (
                <label key={model}>
                  <input
                    type="checkbox"
                    checked={models.includes(model)}
                    onChange={(e) =>
                      setModels((old) =>
                        e.target.checked
                          ? [...old, model]
                          : old.filter((m) => m !== model),
                      )
                    }
                  />
                  {model}
                </label>
              ))}
            </fieldset>
          )}
        </fieldset>
        <p className="evaluation-note">
          开始后，所选问题、标签、检索片段和实验结果（回答模式含生成回答）将上传至
          LangSmith。
          {current?.corpus === 'live'
            ? '当前选择含本机真实资料，请确认这些内容适合上传。'
            : current?.corpus === 'demo'
              ? '合成示例仅用于演示，不代表真实业务质量。'
              : '请先选择测评集。'}
        </p>
        <p>
          默认抽样 3 题，最多 10
          题；不足时使用全部题目。串行执行，同一批问题与检索上下文用于比较。
        </p>
        {mode === 'answers' && (
          <p>
            回答模式会额外产生模型调用费用；每次输出上限{' '}
            {status?.max_output_tokens || 2048} tokens。费用估算按已知 token
            用量与参考单价计算，不是账单；未知用量或单价显示未知。
          </p>
        )}
        <button className="primary-button" disabled={!canStart}>
          {busy ? '正在处理…' : '开始云端实验'}
        </button>
      </form>
      {activeId !== null && (
        <div className="experiment-active">
          <p role="status">
            实验 #{activeId}{' '}
            正在执行。切换页面不会停止后台实验，返回后会恢复进度。
          </p>
          <button disabled={busy} onClick={() => void stop()}>
            停止实验
          </button>
        </div>
      )}
      <details className="experiment-definitions">
        <summary>指标含义与局限</summary>
        <p>
          命中率、Recall、MRR、nDCG
          衡量检索与人工标签的一致性；无答案题正确留空指没有返回检索资料。
        </p>
        <p>
          回答非空、引用编号有效和不确定性措辞属于机械检查，不代表事实正确率。没有引用时引用指标可为不适用；无证据时才检查不确定性措辞。事实准确性仍需人工审阅或参考答案。
        </p>
      </details>
      {job && <ExperimentView job={job} />}
      <section className="evaluation-history">
        <h3>云端实验历史</h3>
        {history.length ? (
          history.map((item) => (
            <button key={item.id} onClick={() => void show(item.id)}>
              查看实验 #{item.id} · {item.dataset_name} ·{' '}
              {statuses[item.status]} · {item.created_at}
            </button>
          ))
        ) : (
          <p>尚无云端实验记录。</p>
        )}
      </section>
    </section>
  )
}
