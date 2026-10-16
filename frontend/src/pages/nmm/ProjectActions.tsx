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
import CandidateComparison from './CandidateComparison'
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
    [zoom, setZoom] = useState<string | null>(null),
    [acknowledgement, setAcknowledgement] = useState<{
      candidate: string
      scope: string
    } | null>(null)
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
  const candidateScope = JSON.stringify([
    project.id,
    project.revision,
    project.candidate_ids,
    project.selected_candidate_id,
  ])
  const original = project.assets.find((asset) => asset.role === 'original')
  useEffect(() => {
    setAcknowledgement(null)
    setZoom(null)
  }, [candidateScope])
  const changed = JSON.stringify(params) !== JSON.stringify(project.params)
  const active =
    ['generating', 'planning', 'rendering'].includes(project.status) ||
    project.active_job_id !== null
  const disabled = busy || !!pending || !!quote || active
  function acknowledged(id: string) {
    return (
      !!original &&
      project.candidate_checks?.[id]?.status === 'needs_review' &&
      acknowledgement?.candidate === id &&
      acknowledgement.scope === candidateScope &&
      !changed
    )
  }
  const quotedCandidate =
    quote?.intent.action === 'select'
      ? quote.intent.candidate
      : quote?.intent.action === 'run'
        ? project.selected_candidate_id
        : null
  const invalidReviewQuote = !!quotedCandidate && !acknowledged(quotedCandidate)
  useEffect(() => {
    if (invalidReviewQuote) setQuote(null)
  }, [invalidReviewQuote])
  async function estimate(intent: Intent) {
    if (gate.current || disabled) return
    const candidate =
      intent.action === 'select'
        ? intent.candidate
        : intent.action === 'run'
          ? project.selected_candidate_id
          : null
    if (candidate && !acknowledged(candidate)) return
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
    const candidate =
      intent.action === 'select'
        ? intent.candidate
        : intent.action === 'run'
          ? project.selected_candidate_id
          : null
    if (candidate && !acknowledged(candidate)) return
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
              ...(candidate ? { quality_acknowledged: true } : {}),
            },
          }
        : {
            kind: 'confirm',
            body: {
              ...base,
              action: intent.action,
              confirm_generation: intent.action === 'regenerate',
              ...(intent.action === 'select'
                ? { quality_acknowledged: true }
                : {}),
              ...(intent.candidate ? { candidate_id: intent.candidate } : {}),
              ...(intent.params ? { params: intent.params } : {}),
            },
          }
    setQuote(null)
    setPending(operation)
    void submit(operation)
  }
  function candidateCard(id: string, index: number, selectable: boolean) {
    const check = project.candidate_checks?.[id]
    const reviewable = check?.status === 'needs_review' && !!original
    return (
      <article
        key={id}
        className="nmm-candidate-card"
        aria-label={`候选 ${index + 1} 结构检查`}
      >
        <button
          type="button"
          aria-label={`对照候选 ${index + 1}`}
          onClick={() => {
            if (acknowledgement?.candidate !== id) setAcknowledgement(null)
            setZoom(id)
          }}
        >
          <img src={assetUrl(id)} alt={`候选效果 ${index + 1}`} />
          <span>对照原图与候选 {index + 1}</span>
        </button>
        <p
          className={check?.status === 'blocked' ? 'nmm-error' : 'nmm-warning'}
        >
          {!check || !['needs_review', 'blocked'].includes(check.status)
            ? '尚未完成结构检查，暂不能选择。请刷新项目；旧版服务需更新后再检查。'
            : check.status === 'blocked'
              ? `局部结构检查发现明显风险，已阻止采用。此检查是启发式判断。${selectable ? '可调整参数后重新生成。' : ''}`
              : '仍需人工对照。自动检查不能保证零件、轮廓和浮雕完整。'}
        </p>
        {!selectable && check?.status === 'blocked' && (
          <p className="nmm-warning">
            此候选不能继续；请返回项目列表新建项目并重新生成。
          </p>
        )}
        {!!check?.reasons.length && (
          <ul className="nmm-candidate-reasons">
            {check.reasons.map((reason, i) => (
              <li key={i}>{reason}</li>
            ))}
          </ul>
        )}
        {!original && (
          <p className="nmm-warning">
            原图暂不可用，无法进行结构对照。请刷新项目。
          </p>
        )}
        {reviewable && (
          <label className="nmm-check">
            <input
              type="checkbox"
              checked={acknowledged(id)}
              disabled={disabled || changed}
              onChange={(event) =>
                setAcknowledgement(
                  event.target.checked
                    ? { candidate: id, scope: candidateScope }
                    : null,
                )
              }
            />
            我已对照原图，确认零件、轮廓和浮雕没有被改动
          </label>
        )}
        {selectable && (
          <button
            disabled={disabled || !acknowledged(id)}
            onClick={() => void estimate({ action: 'select', candidate: id })}
          >
            选择候选 {index + 1}
          </button>
        )}
      </article>
    )
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
      {canRun && project.selected_candidate_id && (
        <>
          <h2>继续前检查已选候选</h2>
          {candidateCard(
            project.selected_candidate_id,
            Math.max(
              0,
              project.candidate_ids.indexOf(project.selected_candidate_id),
            ),
            false,
          )}
        </>
      )}
      {canRun && (
        <button
          className="primary-button"
          disabled={
            disabled ||
            project.provider_mode === 'unconfigured' ||
            (!!project.selected_candidate_id &&
              !acknowledged(project.selected_candidate_id))
          }
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
          <p>
            先对照原图检查结构，再选择候选继续生成教学步骤。不满意时可修改参数并重新估算。
          </p>
          <div className="nmm-candidates">
            {project.candidate_ids.map((id, index) =>
              candidateCard(id, index, true),
            )}
          </div>
          <ParamsForm
            value={params}
            onChange={(value) => {
              setParams(value)
              setAcknowledgement(null)
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
        <NmmDialog title="候选结构对照" onClose={() => setZoom(null)}>
          <CandidateComparison
            key={`${candidateScope}:${zoom}`}
            original={original}
            candidateId={zoom}
          />
        </NmmDialog>
      )}
      {quote && !invalidReviewQuote && (
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
