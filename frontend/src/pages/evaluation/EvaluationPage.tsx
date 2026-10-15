import RetrievalStatus from '../../components/RetrievalStatus'
import RetrievedImages from '../../components/RetrievedImages'
import { useLocation, useNavigate } from 'react-router'
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { api } from '../../api'
import '../../evaluation.css'
import {
  evaluationApi,
  pair,
  type StrategyPair,
  type Diagnostics,
  parseDataset,
  type Dataset,
  type DatasetInput,
  type EvaluationCase,
  type Hit,
  type Metrics,
  type Preview,
  type Report,
  type RunSummary,
} from '../../evaluationApi'
import ExperimentWorkbench from './ExperimentWorkbench'
import DemoDatasetToggle, { firstRealDatasetId } from './DemoDatasetToggle'
const errorText = (e: unknown) =>
  e instanceof Error ? e.message : '操作失败，请重试'
const metricNames = [
  ['hit_rate', '命中率'],
  ['recall', 'Recall'],
  ['mrr', 'MRR'],
  ['ndcg', 'nDCG'],
  ['no_answer_accuracy', '正确留空比例'],
] as const
const formatMetric = (n: number | null) =>
  n === null ? '不适用（分母为 0）' : `${(n * 100).toFixed(1)}%`
const blankCase = (): EvaluationCase => ({
  id: crypto.randomUUID(),
  query: '',
  category: '自定义',
  judgments: [],
})
type ReferenceProps = {
  onOpenReference: (hit: Hit) => void
}
function Hits({
  hits,
  demo,
  onOpenReference,
}: {
  hits: Hit[]
  demo: boolean
} & ReferenceProps) {
  return (
    <div className="evaluation-hits">
      {hits.length ? (
        hits.map((hit) => (
          <article key={hit.key}>
            <div className="evaluation-hit-title">
              <span>#{hit.rank}</span>
              {demo ? (
                <strong>{hit.title}</strong>
              ) : (
                <button onClick={() => onOpenReference(hit)}>
                  {hit.title}
                </button>
              )}
              <small>分数 {hit.score.toFixed(3)}</small>
            </div>
            <p>{hit.excerpt}</p>
            {!demo && <RetrievedImages citation={hit} />}
            <ul>
              {hit.reasons.map((r, i) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
            <div className="evaluation-sources">
              {hit.sources.map((s) =>
                !demo && s.url && /^https?:\/\//i.test(s.url) ? (
                  <a key={s.id} href={s.url} target="_blank" rel="noreferrer">
                    {s.title}
                  </a>
                ) : (
                  <span key={s.id}>{s.title}</span>
                ),
              )}
            </div>
          </article>
        ))
      ) : (
        <p className="evaluation-empty">没有匹配资料</p>
      )}
    </div>
  )
}
function Comparison({
  baseline,
  ranked,
  demo,
  bm25,
  hybrid,
  diagnostics,
  onOpenReference,
}: {
  baseline?: Hit[]
  ranked?: Hit[]
  bm25?: Hit[]
  hybrid?: Hit[]
  diagnostics?: Diagnostics
  demo: boolean
} & ReferenceProps) {
  return (
    <div className="evaluation-comparison">
      <section>
        <h3>{bm25 ? 'BM25 · 在线词法' : '基线 · V2'}</h3>
        {diagnostics && <RetrievalStatus metadata={diagnostics.bm25} />}
        <Hits
          hits={bm25 ?? baseline ?? []}
          demo={demo}
          onOpenReference={onOpenReference}
        />
      </section>
      <section>
        <h3>{hybrid ? '混合检索 · 实际策略见诊断' : '新版 · 本地排序'}</h3>
        {diagnostics && <RetrievalStatus metadata={diagnostics.hybrid} />}
        <Hits
          hits={hybrid ?? ranked ?? []}
          demo={demo}
          onOpenReference={onOpenReference}
        />
      </section>
    </div>
  )
}
function MetricTable({ metrics }: { metrics: StrategyPair<Metrics> }) {
  const [baseline, ranked] = pair(metrics)
  const hybrid = 'bm25' in metrics
  return (
    <div className="evaluation-table-wrap">
      <table>
        <thead>
          <tr>
            <th>指标</th>
            <th>{hybrid ? 'BM25' : '基线'}</th>
            <th>{hybrid ? '请求混合（含降级）' : '新版'}</th>
          </tr>
        </thead>
        <tbody>
          {metricNames.map(([key, label]) => (
            <tr key={key}>
              <th>{label}</th>
              <td>{formatMetric(baseline[key])}</td>
              <td>{formatMetric(ranked[key])}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p>
        正例题 {ranked.positive_cases} 道；无答案题 {ranked.no_answer_cases}{' '}
        道。
      </p>
    </div>
  )
}
function ReportView({
  report,
  onOpenReference,
}: {
  report: Report
} & ReferenceProps) {
  return (
    <section className="evaluation-report">
      <h2>
        报告 #{report.id} · {report.dataset_name}
      </h2>
      <p>
        {report.created_at} · k = {report.k} · 语料 {report.corpus_size} 条 ·{' '}
        {report.corpus === 'demo' ? '合成示例语料' : '本机真实资料'}
      </p>
      {report.corpus === 'demo' && (
        <p className="evaluation-note">
          人工编写的合成示例，仅供回归演示；不是业务数据或独立留出集。示例引用不会打开真实资料。
        </p>
      )}
      {report.report_version === 2 && (
        <p className="evaluation-note">
          降级 {report.degraded_cases ?? 0} 题；
          {report.degraded_cases
            ? '混合列包含实际退回词法的结果，不能视为全部完成混合检索。'
            : '本次无词法降级。检索指标不代表回答质量。'}
        </p>
      )}
      <MetricTable metrics={report.metrics} />
      <details>
        <summary>指标定义与报告快照</summary>
        <p>
          命中率：前 k 条至少命中一个正例的题目比例。Recall：每题命中正例数 /
          全部正例数，再取平均。MRR：首个正例排名倒数的平均值。nDCG：以
          (2^相关性 − 1) / log₂(排名 + 1) 累加并除以理想排序得分，再取平均。
        </p>
        <p>
          上述四项仅以正例题为分母。正确留空比例仅以无答案题为分母，检索返回空列表才算正确。无适用题时显示“不适用”。测评只衡量检索，不代表回答事实准确率。相关性
          1 / 2 / 3 表示弱相关 / 相关 / 高度相关。
        </p>
        <p>不可变历史快照：{pair(report.versions).join(' / ')}</p>
        <p className="evaluation-hash">
          测评集哈希：{report.dataset_hash}
          <br />
          语料哈希：{report.corpus_hash}
        </p>
      </details>
      <h3>逐题诊断</h3>
      {report.cases.map((c) => {
        const [baseline, ranked] = pair(c.metrics)
        const regress = metricNames.some(
          ([key]) =>
            baseline[key] !== null &&
            ranked[key] !== null &&
            ranked[key]! < baseline[key]!,
        )
        return (
          <details className="evaluation-case-result" key={c.id}>
            <summary>
              {regress ? '退步 · ' : ''}
              {c.query.trim() || '空查询'} <small>{c.category}</small>
            </summary>
            <p>
              预期：
              {c.judgments.length
                ? c.judgments.map((j) => `${j.key}（${j.grade} 级）`).join('、')
                : '无答案'}
            </p>
            <MetricTable metrics={c.metrics} />
            <Comparison
              {...c}
              demo={report.corpus === 'demo'}
              onOpenReference={onOpenReference}
            />
          </details>
        )
      })}
    </section>
  )
}
export default function EvaluationWorkbench({
  onOpenReference,
}: ReferenceProps) {
  const location = useLocation()
  const navigate = useNavigate()
  const tab =
    location.pathname === '/evaluation-experiments' ||
    location.pathname === '/evaluation/experiments'
      ? 'experiments'
      : location.pathname === '/evaluation/datasets'
        ? 'datasets'
        : 'preview'
  const setTab = (next: 'preview' | 'datasets' | 'experiments') => {
    void navigate(next === 'preview' ? '/evaluation' : `/evaluation/${next}`)
  }
  const [historyExpanded, setHistoryExpanded] = useState(tab === 'experiments')
  const [showDemo, setShowDemo] = useState(false)
  const [hybrid, setHybrid] = useState(false)
  useEffect(() => {
    if (tab === 'experiments') setHistoryExpanded(true)
  }, [tab])
  const [query, setQuery] = useState(''),
    [k, setK] = useState(5),
    [preview, setPreview] = useState<Preview | null>(null)
  const [datasets, setDatasets] = useState<Dataset[]>([]),
    [selected, setSelected] = useState(''),
    [history, setHistory] = useState<RunSummary[]>([]),
    [report, setReport] = useState<Report | null>(null)
  const [loading, setLoading] = useState(true),
    [busy, setBusy] = useState(false),
    [reportLoading, setReportLoading] = useState(false),
    [error, setError] = useState(''),
    [notice, setNotice] = useState(''),
    [editor, setEditor] = useState<{
      id?: string
      data: DatasetInput
    } | null>(null)
  const alive = useRef(true),
    reportRequest = useRef(0),
    previewRequest = useRef(0),
    datasetRequest = useRef(0)
  useEffect(() => {
    alive.current = true
    Promise.all([evaluationApi.datasets(), evaluationApi.history()])
      .then(([d, h]) => {
        if (alive.current) {
          setDatasets(d)
          setSelected(firstRealDatasetId(d))
          setHistory(h)
        }
      })
      .catch((e) => {
        if (alive.current) setError(errorText(e))
      })
      .finally(() => {
        if (alive.current) setLoading(false)
      })
    return () => {
      alive.current = false
      reportRequest.current++
      previewRequest.current++
      datasetRequest.current++
    }
  }, [])
  async function search(e: FormEvent) {
    e.preventDefault()
    const token = ++previewRequest.current
    setError('')
    setPreview(null)
    setBusy(true)
    try {
      const result = await (hybrid
        ? evaluationApi.hybridPreview(query.trim(), k)
        : evaluationApi.preview(query.trim(), k))
      if (alive.current && token === previewRequest.current) setPreview(result)
    } catch (e) {
      if (alive.current && token === previewRequest.current)
        setError(errorText(e))
    } finally {
      if (alive.current && token === previewRequest.current) setBusy(false)
    }
  }
  async function showReport(id?: number) {
    if (
      id === undefined &&
      (!current || (current.corpus === 'demo' && !showDemo))
    )
      return
    const token = ++reportRequest.current
    setReportLoading(true)
    setReport(null)
    setError('')
    try {
      const result =
        id === undefined
          ? await evaluationApi.run(
              selected,
              k,
              hybrid && current?.corpus === 'live',
            )
          : await evaluationApi.report(id)
      if (alive.current && token === reportRequest.current) {
        setReport(result)
        if (id === undefined)
          setHistory((h) =>
            [result, ...h.filter((r) => r.id !== result.id)].slice(0, 50),
          )
      }
    } catch (e) {
      if (alive.current && token === reportRequest.current)
        setError(errorText(e))
    } finally {
      if (alive.current && token === reportRequest.current)
        setReportLoading(false)
    }
  }
  async function edit() {
    const token = ++datasetRequest.current
    setBusy(true)
    setError('')
    try {
      const d = await evaluationApi.dataset(selected)
      if (alive.current && token === datasetRequest.current)
        setEditor({
          id: d.id,
          data: {
            name: d.name,
            description: d.description,
            cases: d.cases || [],
          },
        })
    } catch (e) {
      if (alive.current && token === datasetRequest.current)
        setError(errorText(e))
    } finally {
      if (alive.current && token === datasetRequest.current) setBusy(false)
    }
  }
  async function remove() {
    if (!window.confirm('删除此测评集？已有历史报告仍会保留。')) return
    setBusy(true)
    setError('')
    try {
      await evaluationApi.remove(selected)
      if (alive.current) {
        const next = datasets.filter((d) => d.id !== selected)
        setDatasets(next)
        setSelected(firstRealDatasetId(next))
        setNotice('测评集已删除')
      }
    } catch (e) {
      if (alive.current) setError(errorText(e))
    } finally {
      if (alive.current) setBusy(false)
    }
  }
  async function exportSet() {
    setBusy(true)
    setError('')
    try {
      const d = await evaluationApi.dataset(selected)
      if (!alive.current) return
      // Live exports contain only accepted input fields; compact encoding preserves the request-size boundary.
      // Demo metadata must survive export so importing it cannot bind synthetic IDs to live records.
      const exported =
        d.corpus === 'demo' || d.builtin
          ? d
          : {
              name: d.name,
              cases: (d.cases || []).map((c) => ({
                id: c.id,
                query: c.query,
                category: c.category,
                judgments: c.judgments.map((j) => ({
                  key: j.key,
                  grade: j.grade,
                })),
              })),
              ...(d.description ? { description: d.description } : {}),
            }
      const url = URL.createObjectURL(
        new Blob([JSON.stringify(exported)], { type: 'application/json' }),
      )
      const a = document.createElement('a')
      a.href = url
      a.download = `evaluation-${d.id}.json`
      a.click()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (e) {
      if (alive.current) setError(errorText(e))
    } finally {
      if (alive.current) setBusy(false)
    }
  }
  const current = datasets.find((d) => d.id === selected)
  return (
    <section className="evaluation-workbench">
      <div className="page-intro">
        <div>
          <h1>检索与测评</h1>
          <p>
            核对真实资料的检索排序，留下可复现的测评记录。旧版词法对比无需模型密钥；混合检索会调用向量服务。
          </p>
        </div>
      </div>
      <nav className="evaluation-tabs" aria-label="测评工作区">
        <button
          aria-pressed={tab === 'preview'}
          onClick={() => setTab('preview')}
        >
          检索调试
        </button>
        <button
          aria-pressed={tab === 'datasets'}
          onClick={() => setTab('datasets')}
        >
          测评集
        </button>
      </nav>
      <details
        className="evaluation-legacy"
        open={historyExpanded}
        onToggle={(event) => setHistoryExpanded(event.currentTarget.open)}
      >
        <summary>历史实验（冻结维护）</summary>
        <p>
          保留既有实验调用与历史记录。日常检索调试和真实资料测评请使用上方入口。
        </p>
        <button
          aria-pressed={tab === 'experiments'}
          onClick={() => setTab('experiments')}
        >
          LangSmith 实验
        </button>
      </details>
      {error && (
        <div role="alert" className="alert error">
          {error}
        </div>
      )}
      {notice && <p role="status">{notice}</p>}
      {tab === 'experiments' ? (
        <ExperimentWorkbench />
      ) : (
        <>
          <label className="evaluation-k">
            返回条数 k
            <select value={k} onChange={(e) => setK(Number(e.target.value))}>
              {Array.from({ length: 20 }, (_, i) => (
                <option key={i + 1}>{i + 1}</option>
              ))}
            </select>
          </label>
          <label className="evaluation-note">
            <input
              type="checkbox"
              aria-label="比较 BM25 与混合检索（调用向量服务）"
              checked={
                hybrid && (tab === 'preview' || current?.corpus !== 'demo')
              }
              disabled={
                busy ||
                reportLoading ||
                (tab === 'datasets' && current?.corpus !== 'live')
              }
              onChange={(event) => {
                setHybrid(event.target.checked)
                setPreview(null)
                previewRequest.current++
              }}
            />
            比较 BM25 与混合检索（调用向量服务）
          </label>
          {hybrid && (tab === 'preview' || current?.corpus === 'live') && (
            <p className="evaluation-note">
              会为问题生成查询向量，发送至已配置服务，可能产生费用。未配置或失败时会明确显示词法降级。
            </p>
          )}
          {tab === 'preview' ? (
            <>
              <form className="evaluation-query" onSubmit={search}>
                <label>
                  检索问题
                  <input
                    maxLength={2000}
                    value={query}
                    onChange={(e) => {
                      setQuery(e.target.value)
                      previewRequest.current++
                      setPreview(null)
                      setBusy(false)
                    }}
                    placeholder="例如：水口打磨或产品编号"
                  />
                </label>
                <button
                  className="primary-button"
                  disabled={busy || !query.trim()}
                >
                  对比检索
                </button>
              </form>
              <p className="evaluation-muted">
                排名分数不是概率；同义词匹配不能确认材质兼容性。
              </p>
              {busy && <p role="status">正在检索…</p>}
              {preview && (
                <>
                  <p>
                    “{preview.query}” · 本机语料 {preview.corpus_size} 条 ·{' '}
                    {preview.version} · k = {preview.k}
                  </p>
                  <Comparison
                    {...preview}
                    demo={false}
                    onOpenReference={onOpenReference}
                  />
                </>
              )}
            </>
          ) : (
            <>
              {loading ? (
                <p role="status">正在读取测评集…</p>
              ) : (
                <>
                  <div className="evaluation-controls">
                    <label>
                      选择测评集
                      <select
                        value={selected}
                        disabled={busy || !!editor}
                        onChange={(e) => {
                          datasetRequest.current++
                          setSelected(e.target.value)
                          setNotice('')
                        }}
                      >
                        <option value="">请选择测评集</option>
                        {datasets
                          .filter((d) => showDemo || d.corpus === 'live')
                          .map((d) => (
                            <option value={d.id} key={d.id}>
                              {d.name}（{d.case_count} 题，
                              {d.corpus === 'demo' ? '合成示例' : '本机资料'}）
                            </option>
                          ))}
                      </select>
                    </label>
                    <button
                      className="primary-button"
                      disabled={!selected || busy || reportLoading || !!editor}
                      onClick={() => void showReport()}
                    >
                      运行测评
                    </button>
                  </div>
                  <DemoDatasetToggle
                    checked={showDemo}
                    disabled={busy || !!editor}
                    onChange={(enabled) => {
                      setShowDemo(enabled)
                      if (!enabled && current?.corpus === 'demo') {
                        datasetRequest.current++
                        setSelected(firstRealDatasetId(datasets))
                      }
                    }}
                  />
                  {!firstRealDatasetId(datasets) && (
                    <p className="evaluation-note">
                      暂无真实资料测评集。请新建测评集，使用本机资料和人工标签核对检索结果。
                    </p>
                  )}
                  {current && <p>{current.description}</p>}
                  <div className="evaluation-actions">
                    <button
                      disabled={busy || !!editor}
                      onClick={() => {
                        setNotice('')
                        setError('')
                        setEditor({
                          data: {
                            name: '',
                            description: '',
                            cases: [blankCase()],
                          },
                        })
                      }}
                    >
                      新建测评集
                    </button>
                    <button
                      disabled={
                        !selected || current?.builtin || busy || !!editor
                      }
                      onClick={() => void edit()}
                    >
                      编辑测评集
                    </button>
                    <button
                      disabled={!selected || busy}
                      onClick={() => void exportSet()}
                    >
                      导出 JSON
                    </button>
                    <button
                      disabled={
                        !selected || current?.builtin || busy || !!editor
                      }
                      onClick={() => void remove()}
                    >
                      删除测评集
                    </button>
                  </div>
                  {current?.corpus === 'demo' && (
                    <p className="evaluation-note">
                      内置集使用隔离的合成示例语料。真实资料测评请新建自定义集。
                    </p>
                  )}
                </>
              )}
              {editor && (
                <DatasetEditor
                  key={editor.id || 'new'}
                  initial={editor.data}
                  onCancel={() => setEditor(null)}
                  onSave={async (data) => {
                    const d = await evaluationApi.save(data, editor.id)
                    if (alive.current) {
                      setDatasets((items) => [
                        ...items.filter((i) => i.id !== d.id),
                        d,
                      ])
                      setSelected(d.id)
                      setEditor(null)
                      setNotice('测评集已保存')
                    }
                  }}
                />
              )}
              <section className="evaluation-history">
                <h2>运行历史</h2>
                <p>
                  历史报告保留当时的题目、语料和算法版本，不随编辑或删除而变化。
                </p>
                {history.length ? (
                  history.map((r) => (
                    <button key={r.id} onClick={() => void showReport(r.id)}>
                      查看报告 #{r.id} · {r.dataset_name} · {r.created_at} · k=
                      {r.k}
                    </button>
                  ))
                ) : (
                  <p>尚无历史记录。选择测评集后运行一次。</p>
                )}
              </section>
              {reportLoading && <p role="status">正在加载报告…</p>}
              {report && (
                <ReportView report={report} onOpenReference={onOpenReference} />
              )}
            </>
          )}
        </>
      )}
    </section>
  )
}
function DatasetEditor({
  initial,
  onCancel,
  onSave,
}: {
  initial: DatasetInput
  onCancel: () => void
  onSave: (data: DatasetInput) => Promise<void>
}) {
  const [data, setData] = useState(initial),
    [raw, setRaw] = useState(''),
    [error, setError] = useState(''),
    [saving, setSaving] = useState(false),
    [records, setRecords] = useState<
      {
        key: string
        title: string
      }[]
    >([]),
    [recordError, setRecordError] = useState(''),
    [recordLoading, setRecordLoading] = useState(true)
  useEffect(() => {
    let active = true
    Promise.all([api.techniques(), api.products(), api.sources()])
      .then(([t, p, s]) => {
        if (active) {
          const sources = new Set(
            s
              .filter((x) => !x.status || x.status === 'published')
              .map((x) => x.id),
          )
          const valid = (x: {
            status: string
            visibility?: string
            source_ids: number[]
          }) =>
            x.status === 'published' &&
            x.source_ids.some((id) => sources.has(id))
          setRecords([
            ...t
              .filter(valid)
              .map((x) => ({ key: `technique:${x.id}`, title: x.title })),
            ...p.filter(valid).map((x) => ({
              key: `product:${x.id}`,
              title: `${x.brand} ${x.name}`,
            })),
          ])
        }
      })
      .catch((e) => {
        if (active) setRecordError(errorText(e))
      })
      .finally(() => {
        if (active) setRecordLoading(false)
      })
    return () => {
      active = false
    }
  }, [])
  function changeCase(index: number, next: EvaluationCase) {
    setData((d) => ({
      ...d,
      cases: d.cases.map((c, i) => (i === index ? next : c)),
    }))
  }
  function load(raw: string) {
    try {
      setData(parseDataset(raw))
      setError('')
    } catch (e) {
      setError(errorText(e))
    }
  }
  async function save(e: FormEvent) {
    e.preventDefault()
    setError('')
    try {
      const parsed = parseDataset(JSON.stringify(data))
      setSaving(true)
      await onSave(parsed)
    } catch (e) {
      setError(errorText(e))
    } finally {
      setSaving(false)
    }
  }
  return (
    <section className="evaluation-editor">
      <h2>自定义测评集 · 本机资料</h2>
      {error && (
        <p className="alert error" role="alert">
          {error}
        </p>
      )}
      <form onSubmit={save}>
        <fieldset disabled={saving}>
          <label>
            测评集名称
            <input
              required
              value={data.name}
              onChange={(e) => setData({ ...data, name: e.target.value })}
            />
          </label>
          <label>
            说明
            <textarea
              value={data.description}
              onChange={(e) =>
                setData({ ...data, description: e.target.value })
              }
            />
          </label>
          {recordLoading && <p>正在读取可用资料…</p>}
          {recordError && <p role="alert">资料选择器：{recordError}</p>}
          {data.cases.map((c, index) => (
            <fieldset className="evaluation-case-editor" key={c.id}>
              <legend>第 {index + 1} 题</legend>
              <label>
                问题 {index + 1}
                <input
                  maxLength={2000}
                  placeholder="留空可测试空查询"
                  value={c.query}
                  onChange={(e) =>
                    changeCase(index, { ...c, query: e.target.value })
                  }
                />
              </label>
              <label>
                分类 {index + 1}
                <input
                  value={c.category}
                  onChange={(e) =>
                    changeCase(index, { ...c, category: e.target.value })
                  }
                />
              </label>
              <p>
                {c.judgments.length
                  ? '可添加多个正例，等级越高越相关。'
                  : '未选择正例：预期无答案，检索应返回空列表。'}
              </p>
              {c.judgments.map((j) => (
                <div className="evaluation-judgment" key={j.key}>
                  <span>
                    {records.find((r) => r.key === j.key)?.title ||
                      `${j.key}（未找到可用资料；运行时会校验）`}
                  </span>
                  <select
                    aria-label={`第 ${index + 1} 题 ${j.key} 相关等级`}
                    value={j.grade}
                    onChange={(e) =>
                      changeCase(index, {
                        ...c,
                        judgments: c.judgments.map((old) =>
                          old.key === j.key
                            ? {
                                ...old,
                                grade: Number(e.target.value) as 1 | 2 | 3,
                              }
                            : old,
                        ),
                      })
                    }
                  >
                    <option value={1}>1 弱相关</option>
                    <option value={2}>2 相关</option>
                    <option value={3}>3 高度相关</option>
                  </select>
                  <button
                    type="button"
                    onClick={() =>
                      changeCase(index, {
                        ...c,
                        judgments: c.judgments.filter(
                          (old) => old.key !== j.key,
                        ),
                      })
                    }
                  >
                    移除正例
                  </button>
                </div>
              ))}
              <label>
                添加正例 {index + 1}
                <select
                  value=""
                  disabled={recordLoading}
                  onChange={(e) => {
                    if (e.target.value)
                      changeCase(index, {
                        ...c,
                        judgments: [
                          ...c.judgments,
                          { key: e.target.value, grade: 3 },
                        ],
                      })
                  }}
                >
                  <option value="">选择已发布资料…</option>
                  {records
                    .filter((r) => !c.judgments.some((j) => j.key === r.key))
                    .map((r) => (
                      <option key={r.key} value={r.key}>
                        {r.title}（{r.key}）
                      </option>
                    ))}
                </select>
              </label>
              <button
                type="button"
                onClick={() =>
                  setData({
                    ...data,
                    cases: data.cases.filter((_, i) => i !== index),
                  })
                }
              >
                删除第 {index + 1} 题
              </button>
            </fieldset>
          ))}
          <div className="evaluation-actions">
            <button
              type="button"
              disabled={data.cases.length >= 200}
              onClick={() =>
                setData({ ...data, cases: [...data.cases, blankCase()] })
              }
            >
              添加题目
            </button>
            <button className="primary-button">
              {saving ? '保存中…' : '保存测评集'}
            </button>
            <button type="button" onClick={onCancel}>
              取消编辑
            </button>
          </div>
        </fieldset>
      </form>
      <details open>
        <summary>JSON 导入</summary>
        <p>
          载入为草稿，检查后保存。空 judgments 数组明确表示无答案。最多 1
          MiB、200 题。
        </p>
        <label>
          导入 JSON 内容
          <textarea
            disabled={saving}
            value={raw}
            onChange={(e) => setRaw(e.target.value)}
          />
        </label>
        <button disabled={saving} onClick={() => load(raw)}>
          载入 JSON 草稿
        </button>
        <label>
          选择 JSON 文件
          <input
            type="file"
            accept=".json,application/json"
            disabled={saving}
            onChange={async (e) => {
              const file = e.target.files?.[0]
              if (!file) return
              if (file.size > 1024 * 1024) {
                setError('JSON 不得超过 1 MiB')
                return
              }
              try {
                load(await file.text())
              } catch (err) {
                setError(errorText(err))
              }
              e.target.value = ''
            }}
          />
        </label>
      </details>
    </section>
  )
}
