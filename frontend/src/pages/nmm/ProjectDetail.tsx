import { useState } from 'react'
import { Link, useParams } from 'react-router'
import { useQueryClient } from '@tanstack/react-query'
import { nmmApi } from '../../nmmApi'
import { statusLabel } from './ProjectList'
import ProjectActions from './ProjectActions'
import ProjectResults from './ProjectResults'
import { useProject } from './useProject'
const phaseLabel: Record<string, string> = {
  analyze: '分析模型与光源',
  generate: '生成候选效果',
  palette: '提取金属色阶',
  plan: '编写涂装步骤',
  render: '绘制过程图',
  refine: '细化教学说明',
  review: '检查结果与风险',
}
export default function ProjectDetail() {
  const { projectId } = useParams()
  return <ProjectDetailContent key={projectId} id={projectId!} />
}
function ProjectDetailContent({ id }: { id: string }) {
  const query = useProject(id),
    cache = useQueryClient(),
    [stopping, setStopping] = useState(false),
    [error, setError] = useState('')
  function refresh() {
    void query.refetch()
    void cache.invalidateQueries({ queryKey: ['nmm', 'list'] })
  }
  async function stop() {
    if (stopping) return
    setStopping(true)
    setError('')
    try {
      const project = await nmmApi.stop(id)
      cache.setQueryData(['nmm', 'project', id], project)
      void cache.invalidateQueries({ queryKey: ['nmm', 'list'] })
    } catch (err) {
      setError(
        err instanceof Error ? err.message : '停止失败，请刷新查看当前状态。',
      )
    } finally {
      setStopping(false)
    }
  }
  if (query.isPending) return <p role="status">正在读取项目…</p>
  if (query.isError && !query.data)
    return (
      <section role="alert">
        <p>项目读取失败：{query.error.message}</p>
        <button onClick={refresh}>重新读取</button>
        <Link to="/nmm">返回列表</Link>
      </section>
    )
  const project = query.data!
  const active = ['generating', 'planning', 'rendering'].includes(
    project.status,
  )
  return (
    <>
      <header className="nmm-heading">
        <div>
          <Link to="/nmm">← 项目列表</Link>
          <h1>{project.title}</h1>
          <p>
            版本 {project.revision} ·{' '}
            {project.steps.length > 0
              ? `请求 ${project.params.step_count} 步 · 实际 ${project.steps.length} 步`
              : `目标 ${project.params.step_count} 步`}{' '}
            · <span className="nmm-status">{statusLabel[project.status]}</span>
          </p>
        </div>
        <div className="nmm-actions">
          <button disabled={query.isFetching} onClick={refresh}>
            刷新状态
          </button>
          {active && (
            <button disabled={stopping} onClick={() => void stop()}>
              {stopping ? '正在停止…' : '停止任务'}
            </button>
          )}
        </div>
      </header>
      {query.disconnected && active && (
        <p className="nmm-warning" role="status">
          进度连接中断，正在定时读取状态。不会重新提交生成请求。
        </p>
      )}
      {query.isError && query.data && (
        <p role="alert" className="nmm-error">
          最新状态读取失败，请检查连接后刷新。
        </p>
      )}
      {error && (
        <p role="alert" className="nmm-error">
          {error}
        </p>
      )}
      {active && (
        <div className="nmm-progress" role="status">
          <span className="nmm-pulse" />
          {phaseLabel[project.phase] || statusLabel[project.status]}
          。您可以离开页面，稍后返回查看。停止会阻止后续调用，已发送的调用仍可能计费。
        </div>
      )}
      {(project.error || project.meta.error_info) && (
        <p className="nmm-error" role="alert">
          {project.meta.error_info?.message || project.error}
          {project.meta.error_info?.retryable === false
            ? ' 请检查配置或创建新项目后再试。'
            : ''}
        </p>
      )}
      {project.warnings.map((warning, i) => (
        <p key={i} className="nmm-warning">
          {warning}
        </p>
      ))}
      <div className="nmm-workspace">
        <ProjectResults key={`${id}-${project.revision}`} project={project} />
        <aside className="nmm-controls">
          <ProjectActions project={project} onRefresh={refresh} />
          <section className="nmm-usage">
            <h2>调用记录</h2>
            {project.provider_mode === 'unconfigured' ? (
              <p>模型未配置</p>
            ) : (
              <>
                <p>
                  已预留图片 {project.meta.usage.reserved_image_calls} 次，视觉{' '}
                  {project.meta.usage.reserved_vision_calls} 次。
                </p>
                {Object.entries(project.meta.usage.known_costs).map(
                  ([currency, cost]) => (
                    <p key={currency}>
                      已知费用小计：{cost} {currency}
                    </p>
                  ),
                )}
                {project.meta.usage.unknown_cost_calls > 0 && (
                  <p>
                    另有 {project.meta.usage.unknown_cost_calls}{' '}
                    次调用费用未知。
                  </p>
                )}
                {Object.keys(project.meta.usage.known_costs).length === 0 && (
                  <p>暂无已知费用小计；不代表费用为零。</p>
                )}
                {project.meta.authorization &&
                  !project.meta.authorization.disabled && (
                    <p>
                      当前授权剩余：图片{' '}
                      {project.meta.authorization.remaining_image_calls}{' '}
                      次，视觉{' '}
                      {project.meta.authorization.remaining_vision_calls} 次。
                    </p>
                  )}
              </>
            )}
          </section>
        </aside>
      </div>
    </>
  )
}
