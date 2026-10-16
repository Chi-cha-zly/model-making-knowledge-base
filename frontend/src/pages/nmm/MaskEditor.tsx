import { useEffect, useId, useRef, useState } from 'react'
import type { PointerEvent as ReactPointerEvent } from 'react'
import {
  BinaryMask,
  exportMask,
  imagePoint,
  loadRaster,
  paintOverlay,
  putRaster,
  readBinaryMask,
  validateOriginal,
} from './maskCanvas'
import type { Point, Raster } from './maskCanvas'
import './mask.css'

type Props = {
  file: File
  onChange: (mask: Blob | null) => void
  disabled?: boolean
}

export default function MaskEditor({
  file,
  onChange,
  disabled = false,
}: Props) {
  const id = useId()
  const source = useRef<HTMLCanvasElement>(null)
  const overlay = useRef<HTMLCanvasElement>(null)
  const model = useRef<BinaryMask | null>(null)
  const stroke = useRef<{ pointer: number; point: Point } | null>(null)
  const upload = useRef<AbortController | null>(null)
  const epoch = useRef(0)
  const callback = useRef(onChange)
  callback.current = onChange
  const [raster, setRaster] = useState<Raster | null>(null)
  const [error, setError] = useState('')
  const [tool, setTool] = useState<'paint' | 'erase'>('paint')
  const [size, setSize] = useState(32)
  const [opacity, setOpacity] = useState(45)
  const [busy, setBusy] = useState(false)
  const [canUndo, setCanUndo] = useState(false)
  const [selected, setSelected] = useState(false)
  const [status, setStatus] = useState('正在读取原图…')

  function invalidate() {
    epoch.current++
    callback.current(null)
  }
  function fail(reason: unknown) {
    setError(
      reason instanceof Error ? reason.message : '遮罩处理失败，请重试。',
    )
  }

  useEffect(() => {
    const controller = new AbortController()
    const token = ++epoch.current
    callback.current(null)
    upload.current?.abort()
    stroke.current = null
    model.current = null
    setRaster(null)
    setError('')
    setSelected(false)
    setCanUndo(false)
    setBusy(false)
    setStatus('正在读取原图…')
    loadRaster(file, controller.signal)
      .then((value) => {
        if (controller.signal.aborted || token !== epoch.current) return
        validateOriginal(value.width, value.height, file.size, value.data)
        model.current = new BinaryMask(value.width, value.height)
        setRaster(value)
        setStatus('请涂出需要修改的区域，红色区域将被编辑。')
      })
      .catch((reason) => {
        if (!controller.signal.aborted && token === epoch.current) {
          fail(reason)
          setStatus('原图无法使用。')
        }
      })
    return () => {
      epoch.current++
      controller.abort()
      upload.current?.abort()
      stroke.current = null
    }
  }, [file])

  useEffect(() => {
    if (!raster || !source.current || !overlay.current || !model.current) return
    try {
      putRaster(source.current, raster)
      overlay.current.width = raster.width
      overlay.current.height = raster.height
      paintOverlay(overlay.current, model.current)
    } catch (reason) {
      fail(reason)
      model.current = null
      setRaster(null)
      setStatus('当前浏览器无法编辑遮罩。')
      callback.current(null)
    }
  }, [raster])

  function publish() {
    const mask = model.current
    if (!mask) return
    invalidate()
    const token = epoch.current
    setCanUndo(mask.canUndo)
    setSelected(mask.hasSelection)
    if (!mask.hasSelection) {
      setStatus('遮罩为空，请绘制区域或上传黑白遮罩。')
      return
    }
    setStatus('正在准备遮罩…')
    exportMask(mask)
      .then((blob) => {
        if (token !== epoch.current) return
        callback.current(blob)
        setStatus('遮罩已就绪。')
      })
      .catch((reason) => {
        if (token === epoch.current) {
          fail(reason)
          setStatus('遮罩尚未就绪。')
        }
      })
  }

  function point(event: ReactPointerEvent<HTMLCanvasElement>) {
    const mask = model.current!
    return imagePoint(
      event.clientX,
      event.clientY,
      event.currentTarget.getBoundingClientRect(),
      mask.width,
      mask.height,
    )
  }

  function start(event: ReactPointerEvent<HTMLCanvasElement>) {
    if (
      disabled ||
      busy ||
      !model.current ||
      stroke.current ||
      event.button !== 0
    )
      return
    event.preventDefault()
    invalidate()
    setError('')
    event.currentTarget.setPointerCapture?.(event.pointerId)
    const at = point(event)
    model.current.begin()
    stroke.current = { pointer: event.pointerId, point: at }
    const box = model.current.paint(at, at, size / 2, tool === 'erase')
    paintOverlay(event.currentTarget, model.current, box)
    setStatus('正在编辑，松开后保存遮罩。')
  }

  function move(event: ReactPointerEvent<HTMLCanvasElement>) {
    if (
      !stroke.current ||
      event.pointerId !== stroke.current.pointer ||
      !model.current
    )
      return
    const at = point(event)
    const box = model.current.paint(
      stroke.current.point,
      at,
      size / 2,
      tool === 'erase',
    )
    stroke.current.point = at
    paintOverlay(event.currentTarget, model.current, box)
  }

  function finish(event: ReactPointerEvent<HTMLCanvasElement>) {
    if (!stroke.current || event.pointerId !== stroke.current.pointer) return
    stroke.current = null
    model.current?.end()
    if (event.currentTarget.hasPointerCapture?.(event.pointerId))
      event.currentTarget.releasePointerCapture(event.pointerId)
    publish()
  }

  function edit(action: 'clear' | 'undo') {
    if (disabled || busy || !model.current) return
    stroke.current = null
    model.current.end()
    if (action === 'clear') model.current.clear()
    else model.current.undo()
    if (overlay.current) paintOverlay(overlay.current, model.current)
    setError('')
    publish()
  }

  async function importMask(file: File) {
    if (disabled || busy || !model.current) return
    upload.current?.abort()
    const controller = new AbortController()
    upload.current = controller
    invalidate()
    const token = epoch.current
    setBusy(true)
    setError('')
    try {
      const value = await loadRaster(file, controller.signal)
      if (
        controller.signal.aborted ||
        token !== epoch.current ||
        !model.current
      )
        return
      model.current.replace(
        readBinaryMask(
          value.data,
          value.width,
          value.height,
          model.current.width,
          model.current.height,
        ),
      )
      if (overlay.current) paintOverlay(overlay.current, model.current)
      publish()
    } catch (reason) {
      if (!controller.signal.aborted && token === epoch.current) {
        fail(reason)
        setStatus('上传未成功，请重新选择或编辑遮罩。')
      }
    } finally {
      if (!controller.signal.aborted) setBusy(false)
    }
  }

  const locked = disabled || busy || !raster
  return (
    <section className="nmm-mask-editor" aria-label="编辑涂装区域">
      <div className="nmm-mask-heading">
        <strong>涂装区域</strong>
        {raster && (
          <span>
            {raster.width} × {raster.height} 像素
          </span>
        )}
      </div>
      <div className="nmm-mask-toolbar" role="group" aria-label="遮罩工具">
        <button
          type="button"
          disabled={locked}
          aria-pressed={tool === 'paint'}
          onClick={() => setTool('paint')}
        >
          画笔
        </button>
        <button
          type="button"
          disabled={locked}
          aria-pressed={tool === 'erase'}
          onClick={() => setTool('erase')}
        >
          擦除
        </button>
        <button
          type="button"
          disabled={locked || !canUndo}
          onClick={() => edit('undo')}
        >
          撤销
        </button>
        <button
          type="button"
          disabled={locked || !selected}
          onClick={() => edit('clear')}
        >
          清空遮罩
        </button>
        <label htmlFor={`${id}-size`}>
          画笔大小 <output>{size} px</output>
          <input
            aria-label="画笔大小"
            id={`${id}-size`}
            type="range"
            min="2"
            max="250"
            value={size}
            disabled={locked}
            onChange={(event) => setSize(Number(event.target.value))}
          />
        </label>
        <label htmlFor={`${id}-opacity`}>
          预览透明度 <output>{opacity}%</output>
          <input
            aria-label="预览透明度"
            id={`${id}-opacity`}
            type="range"
            min="10"
            max="80"
            value={opacity}
            disabled={locked}
            onChange={(event) => setOpacity(Number(event.target.value))}
          />
        </label>
      </div>
      {raster && (
        <div className="nmm-mask-stage">
          <canvas
            ref={source}
            width={raster.width}
            height={raster.height}
            className="nmm-mask-original"
            aria-hidden="true"
          />
          <canvas
            ref={overlay}
            width={raster.width}
            height={raster.height}
            className="nmm-mask-overlay"
            style={{ opacity: opacity / 100 }}
            aria-label="遮罩绘制画布"
            aria-describedby={`${id}-help`}
            aria-disabled={locked}
            onPointerDown={start}
            onPointerMove={move}
            onPointerUp={finish}
            onPointerCancel={finish}
            onLostPointerCapture={finish}
          />
        </div>
      )}
      <p id={`${id}-help`} className="nmm-mask-help">
        在原图上拖动涂出红色区域；仅涂出的区域将被编辑，未涂区域保留原图。也可用下方按钮上传同尺寸的不透明黑白遮罩，白色表示编辑。
      </p>
      <label className="nmm-mask-upload">
        上传黑白遮罩
        <input
          type="file"
          accept="image/png"
          aria-label="上传黑白遮罩"
          disabled={locked}
          onChange={(event) => {
            const next = event.target.files?.[0]
            event.target.value = ''
            if (next) void importMask(next)
          }}
        />
      </label>
      <p className="nmm-mask-status" role="status">
        {status}
      </p>
      {error && (
        <p className="nmm-mask-error" role="alert">
          {error}
        </p>
      )}
    </section>
  )
}
