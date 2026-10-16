import { useEffect, useRef, useState } from 'react'
import { useNavigate, Link } from 'react-router'
import { useQueryClient } from '@tanstack/react-query'
import { nmmApi, type ReferenceRole } from '../../nmmApi'
import MaskEditor from './MaskEditor'
import ParamsForm, { defaultParams, validParams } from './ParamsForm'
import { prepareOriginal, type PreparedOriginal } from './prepareOriginal'
const allowed = ['image/png', 'image/jpeg', 'image/webp']
const roles: [ReferenceRole, string][] = [
  ['reference_style', '效果参考'],
  ['reference_palette', '配色参考'],
  ['reference_steps', '过程参考'],
]
export function validateFile(file: File) {
  if (!allowed.includes(file.type))
    throw new Error('仅支持 PNG、JPEG 或 WebP 图片。')
  if (file.size > 20 * 1024 * 1024) throw new Error('每张图片不能超过 20 MiB。')
}
function Preview({ file }: { file: File }) {
  const [url, setUrl] = useState('')
  useEffect(() => {
    const value = URL.createObjectURL(file)
    setUrl(value)
    return () => URL.revokeObjectURL(value)
  }, [file])
  return <img src={url || undefined} alt={file.name} />
}
export default function NewProject() {
  const navigate = useNavigate(),
    cache = useQueryClient()
  const [title, setTitle] = useState(''),
    [params, setParams] = useState(defaultParams),
    [original, setOriginal] = useState<File | null>(null),
    [mask, setMask] = useState<Blob | null>(null),
    [identity, setIdentity] = useState(0),
    [prepared, setPrepared] = useState<PreparedOriginal | null>(null),
    [selectedName, setSelectedName] = useState(''),
    [preparing, setPreparing] = useState(false),
    [originalError, setOriginalError] = useState('')
  const [references, setReferences] = useState<
      { file: File; role: ReferenceRole }[]
    >([]),
    [error, setError] = useState(''),
    [busy, setBusy] = useState(false),
    [checking, setChecking] = useState(false)
  const mounted = useRef(true),
    refRequest = useRef(0),
    originalRequest = useRef(0),
    originalController = useRef<AbortController | null>(null)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      refRequest.current++
      originalRequest.current++
      originalController.current?.abort()
    }
  }, [])
  async function selectOriginal(file: File) {
    originalController.current?.abort()
    const controller = new AbortController()
    originalController.current = controller
    const request = ++originalRequest.current
    setOriginal(null)
    setMask(null)
    setPrepared(null)
    setSelectedName(file.name)
    setOriginalError('')
    setError('')
    setPreparing(true)
    try {
      validateFile(file)
      const result = await prepareOriginal(file, controller.signal)
      if (
        !mounted.current ||
        controller.signal.aborted ||
        request !== originalRequest.current
      )
        return
      setPrepared(result)
      setOriginal(result.file)
      setIdentity((i) => i + 1)
    } catch (err) {
      if (
        mounted.current &&
        !controller.signal.aborted &&
        request === originalRequest.current
      )
        setOriginalError(
          err instanceof Error ? err.message : '原图无法使用，请重新选择。',
        )
    } finally {
      if (mounted.current && request === originalRequest.current)
        setPreparing(false)
    }
  }
  async function addReferences(files: File[]) {
    const request = ++refRequest.current
    setChecking(true)
    setError('')
    try {
      if (references.length + files.length > 12)
        throw new Error('参考图片最多 12 张。')
      for (const file of files) {
        validateFile(file)
        const bitmap = await createImageBitmap(file)
        try {
          if (bitmap.width * bitmap.height > 20_000_000)
            throw new Error('参考图片不能超过 2000 万像素。')
        } finally {
          bitmap.close()
        }
      }
      if (mounted.current && request === refRequest.current)
        setReferences((current) => [
          ...current,
          ...files.map((file) => ({ file, role: 'reference_steps' as const })),
        ])
    } catch (err) {
      if (mounted.current && request === refRequest.current)
        setError(err instanceof Error ? err.message : '参考图片无法解码。')
    } finally {
      if (mounted.current && request === refRequest.current) setChecking(false)
    }
  }
  function changeRole(index: number, role: ReferenceRole) {
    const next = references.map((ref, i) =>
      i === index ? { ...ref, role } : ref,
    )
    if (next.filter((ref) => ref.role !== 'reference_steps').length > 3) {
      setError('效果参考与配色参考合计最多 3 张。')
      return
    }
    setError('')
    setReferences(next)
  }
  async function save(e: React.FormEvent) {
    e.preventDefault()
    if (
      busy ||
      checking ||
      preparing ||
      !original ||
      !mask ||
      !title.trim() ||
      !validParams(params)
    )
      return
    setBusy(true)
    setError('')
    try {
      const project = await nmmApi.create({
        title: title.trim(),
        params,
        original,
        mask,
        references,
      })
      void cache.invalidateQueries({ queryKey: ['nmm', 'list'] })
      if (mounted.current) navigate(`/nmm/${encodeURIComponent(project.id)}`)
    } catch (err) {
      if (mounted.current)
        setError(err instanceof Error ? err.message : '保存失败，请重试。')
    } finally {
      if (mounted.current) setBusy(false)
    }
  }
  return (
    <>
      <header className="nmm-heading">
        <div>
          <Link to="/nmm">← 项目列表</Link>
          <h1>新建涂装练习</h1>
          <p>先圈出要绘制金属效果的区域，再保存本地项目。</p>
        </div>
      </header>
      <form onSubmit={save} className="nmm-workspace">
        <section className="nmm-stage-panel">
          <label className="nmm-upload">
            原图
            <input
              type="file"
              accept={allowed.join(',')}
              disabled={busy}
              onChange={(e) => {
                const file = e.target.files?.[0]
                if (!file) return
                void selectOriginal(file)
                e.target.value = ''
              }}
            />
          </label>
          <p className="nmm-hint">
            PNG / JPEG / WebP · 输入最大 2000 万像素、20 MiB · 必须不透明。超过
            400 万像素会在本地等比缩小为工作图。
          </p>
          {selectedName && <p className="nmm-hint">已选择：{selectedName}</p>}
          {preparing && <p role="status">正在本地处理原图，请稍候…</p>}
          {originalError && (
            <p className="nmm-error" role="alert">
              {originalError}
            </p>
          )}
          {prepared && (
            <div className="nmm-hint" role="status">
              <p>
                源图 {prepared.sourceWidth} × {prepared.sourceHeight} → 工作图{' '}
                {prepared.width} × {prepared.height} 像素
              </p>
              <p>
                {prepared.resized
                  ? '仅在浏览器本地缩小，未修改源文件；保存时只上传工作图。'
                  : '尺寸无需缩小，保留原文件作为工作图。'}{' '}
                遮罩外保留的是工作图中的对应像素。
              </p>
            </div>
          )}
          {original ? (
            <MaskEditor
              key={identity}
              file={original}
              onChange={setMask}
              disabled={busy}
            />
          ) : (
            <div className="nmm-empty-stage">
              上传模型照片，在需要涂装的区域绘制遮罩。
            </div>
          )}
          <section className="nmm-references">
            <h2>
              参考图片 <small>可选</small>
            </h2>
            <p>
              最多 12 张；效果与配色合计最多 3 张。每张不超过 2000 万像素、20
              MiB。
            </p>
            <label>
              添加参考
              <input
                type="file"
                accept={allowed.join(',')}
                multiple
                disabled={busy || checking}
                onChange={(e) => {
                  void addReferences([...(e.target.files || [])])
                  e.target.value = ''
                }}
              />
            </label>
            {checking && <p role="status">正在检查参考图片…</p>}
            <div className="nmm-reference-grid">
              {references.map((ref, index) => (
                <div key={`${index}-${ref.file.name}`}>
                  <Preview file={ref.file} />
                  <label>
                    参考 {index + 1} 的用途
                    <select
                      value={ref.role}
                      disabled={busy || checking}
                      onChange={(e) =>
                        changeRole(index, e.target.value as ReferenceRole)
                      }
                    >
                      {roles.map(([role, label]) => (
                        <option key={role} value={role}>
                          {label}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button
                    type="button"
                    disabled={busy || checking}
                    onClick={() =>
                      setReferences((refs) =>
                        refs.filter((_, i) => i !== index),
                      )
                    }
                  >
                    移除参考 {index + 1}
                  </button>
                </div>
              ))}
            </div>
          </section>
        </section>
        <aside className="nmm-controls">
          <label>
            项目名称
            <input
              value={title}
              maxLength={300}
              required
              disabled={busy}
              onChange={(e) => setTitle(e.target.value)}
            />
          </label>
          <ParamsForm value={params} onChange={setParams} disabled={busy} />
          {error && (
            <p className="nmm-error" role="alert">
              {error}
            </p>
          )}
          <button
            className="primary-button"
            disabled={
              busy ||
              checking ||
              preparing ||
              !original ||
              !mask ||
              !title.trim() ||
              !validParams(params)
            }
          >
            {busy ? '正在保存…' : '保存本地项目'}
          </button>
          <p className="nmm-hint" role="status">
            {preparing
              ? '请等待原图处理完成。'
              : !original
                ? '请先选择原图并完成本地读取。'
                : !mask
                  ? '请先绘制涂装区域，或上传同尺寸黑白遮罩。'
                  : !title.trim()
                    ? '请填写项目名称后保存。'
                    : !validParams(params)
                      ? '请检查涂装参数：步骤数需为 4–9，金属名称不能为空。'
                      : checking
                        ? '请等待参考图片检查完成。'
                        : '工作图与遮罩已就绪，可以保存本地项目。'}
          </p>
          <p className="nmm-hint">
            保存不会调用模型。下一步先查看估算，再决定是否授权。
          </p>
        </aside>
      </form>
    </>
  )
}
