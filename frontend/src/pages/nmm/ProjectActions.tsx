import { useEffect, useRef, useState } from 'react'
import {
  assetUrl,
  nmmApi,
  NmmApiError,
  type CostAuthorization,
  type NmmConfirmInput,
  type NmmEstimate,
  type NmmParams,
  type NmmProject,
  type NmmRunInput,
} from '../../nmmApi'
import CostDialog from './CostDialog'
import NmmDialog from './NmmDialog'
import ParamsForm, { validParams } from './ParamsForm'
type Intent = {
  action: 'run' | 'select' | 'regenerate'
  candidate?: string
  params?: NmmParams
}
type Operation =
  | { kind: 'run'; body: NmmRunInput }
  | { kind: 'confirm'; body: NmmConfirmInput }
export default function ProjectActions({
  project,
  onRefresh,
}: {
  project: NmmProject
  onRefresh: () => void
}) {
  const [params, setParams] = useState(project.params),
    [quote, setQuote] = useState<{
      estimate: NmmEstimate
      intent: Intent
    } | null>(null),
    [pending, setPending] = useState<Operation | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(''),
    [zoom, setZoom] = useState<string | null>(null)
  const gate = useRef(false),
    mounted = useRef(true),
    revision = useRef(project.revision),
    serial = useRef(0)
  const refresh = useRef(onRefresh)
  refresh.current = onRefresh
  revision.current = project.revision
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      serial.current++
    }
  }, [])
  useEffect(() => {
    setQuote(null)
    setParams(project.params)
    serial.current++
  }, [project.revision, project.params])
  const changed = JSON.stringify(params) !== JSON.stringify(project.params)
  const active =
    ['generating', 'planning', 'rendering'].includes(project.status) ||
    project.active_job_id !== null
  const disabled = busy || !!pending || !!quote || active
  async function estimate(intent: Intent) {
    if (gate.current || disabled) return
    gate.current = true
    setBusy(true)
    setError('')
    const version = project.revision,
      request = ++serial.current
    try {
      const value = await nmmApi.estimate(project.id, intent.params)
      if (
        mounted.current &&
        version === revision.current &&
        request === serial.current
      ) {
        if (value.revision !== version) {
          setError('项目版本已变化，请重新估算。')
          refresh.current()
        } else if (value.provider_mode === 'unconfigured')
          setError('模型尚未配置；可以保留和浏览本地项目。')
        else setQuote({ estimate: value, intent })
      }
    } catch (err) {
      if (mounted.current) {
        setError(err instanceof Error ? err.message : '估算失败。')
        if (err instanceof NmmApiError && err.status === 409) refresh.current()
      }
    } finally {
      gate.current = false
      if (mounted.current) setBusy(false)
    }
  }
  async function submit(operation: Operation) {
    if (gate.current) return
    gate.current = true
    setBusy(true)
    setError('')
    try {
      if (operation.kind === 'run') await nmmApi.run(project.id, operation.body)
      else await nmmApi.confirm(project.id, operation.body)
      if (mounted.current) {
        setPending(null)
        refresh.current()
      }
    } catch (err) {
      if (!mounted.current) return
      if (err instanceof NmmApiError && err.status === 409) {
        setPending(null)
        setError(`${err.message}。请重新估算并确认当前版本。`)
        refresh.current()
      } else if (
        !(err instanceof NmmApiError) ||
        err.status === 0 ||
        err.status >= 500
      ) {
        setPending(operation)
        setError(
          '操作结果暂不确定。请检查连接，使用原操作重试；不会自动重新生成。',
        )
        refresh.current()
      } else {
        setPending(null)
        setError(err.message)
      }
    } finally {
      gate.current = false
      if (mounted.current) setBusy(false)
    }
  }
  function approve(authorization?: CostAuthorization) {
    if (!quote || quote.estimate.revision !== project.revision || gate.current)
      return
    const { intent } = quote
    const base = {
      request_id: crypto.randomUUID(),
      revision: project.revision,
      ...(authorization ? { cost_authorization: authorization } : {}),
    }
    const operation: Operation =
      intent.action === 'run'
        ? {
            kind: 'run',
            body: {
              ...base,
              confirm_generation: quote.estimate.generation_required ?? true,
            },
          }
        : {
            kind: 'confirm',
            body: {
              ...base,
              action: intent.action,
              confirm_generation: intent.action === 'regenerate',
              ...(intent.candidate ? { candidate_id: intent.candidate } : {}),
              ...(intent.params ? { params: intent.params } : {}),
            },
          }
    setQuote(null)
    setPending(operation)
    void submit(operation)
  }
  const canRun = ['draft', 'failed', 'stopped'].includes(project.status)
  return (
    <section className="nmm-project-actions">
      {error && (
        <p className="nmm-error" role="alert">
          {error}
        </p>
      )}
      {pending && !busy && (
        <button onClick={() => void submit(pending)}>重试原操作</button>
      )}
      {project.provider_mode === 'unconfigured' && (
        <p className="nmm-warning">
          模型尚未配置。项目已保存在本地；配置独立的图像和视觉服务后才能生成。
        </p>
      )}
      {canRun && (
        <button
          className="primary-button"
          disabled={disabled || project.provider_mode === 'unconfigured'}
          onClick={() => void estimate({ action: 'run' })}
        >
          {busy
            ? '正在处理…'
            : project.status === 'draft'
              ? '估算并生成候选'
              : '估算并继续任务'}
        </button>
      )}
      {project.status === 'awaiting_confirmation' && (
        <>
          <h2>选择候选效果</h2>
          <p>选择后继续生成教学步骤；不满意时可修改参数并重新估算。</p>
          <div className="nmm-candidates">
            {project.candidate_ids.map((id, index) => (
              <article key={id}>
                <button
                  type="button"
                  aria-label={`放大候选 ${index + 1}`}
                  onClick={() => setZoom(id)}
                >
                  <img src={assetUrl(id)} alt={`候选效果 ${index + 1}`} />
                </button>
                <button
                  disabled={disabled || changed}
                  onClick={() =>
                    void estimate({ action: 'select', candidate: id })
                  }
                >
                  选择候选 {index + 1}
                </button>
              </article>
            ))}
          </div>
          <ParamsForm
            value={params}
            onChange={(value) => {
              setParams(value)
              setQuote(null)
            }}
            disabled={disabled}
          />
          {changed && (
            <p className="nmm-hint">
              参数已修改，重新生成会创建新版本。若要选择当前候选，请先恢复参数。
            </p>
          )}
          {changed && (
            <button
              disabled={disabled}
              onClick={() => setParams(project.params)}
            >
              恢复当前参数
            </button>
          )}
          <button
            disabled={
              disabled || !validParams(params) || project.generation_count >= 6
            }
            onClick={() =>
              void estimate({
                action: 'regenerate',
                ...(changed ? { params } : {}),
              })
            }
          >
            {changed ? '修改参数并重新估算' : '重新估算候选'}
          </button>
        </>
      )}
      {zoom && (
        <NmmDialog title="候选效果预览" onClose={() => setZoom(null)}>
          <img src={assetUrl(zoom)} alt="候选效果放大预览" />
        </NmmDialog>
      )}
      {quote && (
        <CostDialog
          estimate={quote.estimate}
          selection={quote.intent.action === 'select'}
          resuming={
            quote.intent.action === 'run' && !!project.selected_candidate_id
          }
          cachedRecovery={
            quote.intent.action === 'run' &&
            quote.estimate.generation_required === false &&
            !project.selected_candidate_id
          }
          onApprove={approve}
          onClose={() => setQuote(null)}
        />
      )}
    </section>
  )
}
