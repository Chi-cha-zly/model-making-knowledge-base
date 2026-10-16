import { useState } from 'react'
import { Link } from 'react-router'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  assetUrl,
  nmmApi,
  NmmApiError,
  type NmmProjectSummary,
} from '../../nmmApi'
import NmmDialog from './NmmDialog'
export const statusLabel = {
  draft: '待生成',
  generating: '生成候选中',
  awaiting_confirmation: '等待选择候选',
  planning: '规划步骤中',
  rendering: '绘制步骤中',
  done: '已完成',
  failed: '任务失败',
  stopped: '已停止',
}
export default function ProjectList() {
  const cache = useQueryClient(),
    query = useQuery({ queryKey: ['nmm', 'list'], queryFn: nmmApi.list })
  const [deleting, setDeleting] = useState<NmmProjectSummary | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState('')
  async function remove() {
    if (!deleting || busy) return
    setBusy(true)
    setError('')
    try {
      await nmmApi.remove(deleting.id)
      cache.removeQueries({ queryKey: ['nmm', 'project', deleting.id] })
      setDeleting(null)
      await cache.invalidateQueries({ queryKey: ['nmm', 'list'] })
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除失败。')
    } finally {
      setBusy(false)
    }
  }
  return (
    <>
      <header className="nmm-heading">
        <div>
          <p className="nmm-eyebrow">NON-METALLIC METAL</p>
          <h1>NMM 涂装工作台</h1>
          <p>从一张模型照片，逐步练习光影与金属质感。</p>
        </div>
        <Link className="primary-button" to="/nmm/new">
          新建涂装练习
        </Link>
      </header>
      {query.isPending ? (
        <p role="status">正在读取项目…</p>
      ) : query.isError ? (
        <div role="alert">
          <p>
            项目读取失败
            {query.error instanceof NmmApiError && query.error.status > 0
              ? `（HTTP ${query.error.status}）`
              : ''}
            ：{query.error.message}
          </p>
          {query.error instanceof NmmApiError && query.error.status === 404 && (
            <p>
              当前服务可能是旧版后端，或尚未启用 NMM
              接口。请检查前端连接的后端版本与启动地址后重试。
            </p>
          )}
          <button onClick={() => void query.refetch()}>重新读取</button>
        </div>
      ) : query.data.length === 0 ? (
        <div className="nmm-empty-stage">
          <h2>还没有涂装项目</h2>
          <p>上传原图和遮罩，保存后再决定是否使用模型。</p>
          <Link to="/nmm/new">开始第一份练习 →</Link>
        </div>
      ) : (
        <div className="nmm-project-grid">
          {query.data.map((project) => (
            <article className="nmm-project-card" key={project.id}>
              <Link
                to={`/nmm/${encodeURIComponent(project.id)}`}
                className="nmm-project-preview"
              >
                {project.thumbnail_asset_id ? (
                  <img
                    loading="lazy"
                    src={assetUrl(project.thumbnail_asset_id)}
                    alt={project.title}
                  />
                ) : (
                  <span>尚无预览</span>
                )}
              </Link>
              <div>
                <span className="nmm-status">
                  {statusLabel[project.status]}
                </span>
                <h2>
                  <Link to={`/nmm/${encodeURIComponent(project.id)}`}>
                    {project.title}
                  </Link>
                </h2>
                <p>
                  {project.params.metal} · {project.params.step_count} 步 · 版本{' '}
                  {project.revision}
                </p>
                <div className="nmm-actions">
                  <Link to={`/nmm/${encodeURIComponent(project.id)}`}>
                    继续查看 →
                  </Link>
                  <button
                    onClick={() => {
                      setError('')
                      setDeleting(project)
                    }}
                    aria-label={`删除 ${project.title}`}
                  >
                    删除
                  </button>
                </div>
              </div>
            </article>
          ))}
        </div>
      )}
      {deleting && (
        <NmmDialog
          title="删除项目"
          onClose={() => {
            if (!busy) setDeleting(null)
          }}
        >
          <p>确认删除「{deleting.title}」及其图片和步骤？此操作无法撤销。</p>
          {error && (
            <p role="alert" className="nmm-error">
              {error}
            </p>
          )}
          <div className="nmm-actions">
            <button disabled={busy} onClick={() => setDeleting(null)}>
              保留项目
            </button>
            <button disabled={busy} onClick={() => void remove()}>
              {busy ? '正在删除…' : '确认删除'}
            </button>
          </div>
        </NmmDialog>
      )}
    </>
  )
}
