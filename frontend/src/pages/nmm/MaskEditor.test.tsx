import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import MaskEditor from './MaskEditor'
import * as raster from './maskCanvas'

vi.mock('./maskCanvas', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./maskCanvas')>()),
  loadRaster: vi.fn(),
}))

const pendingBlobs: Array<(blob: Blob | null) => void> = []
const pixels = () => new Uint8ClampedArray([10, 20, 30, 255, 40, 50, 60, 255])
const loaded = (width = 2, height = 1, data = pixels()) => ({
  width,
  height,
  data,
})
const original = () =>
  new File(['image'], 'original.png', { type: 'image/png' })
let renderedPixels: Uint8ClampedArray[]

beforeEach(() => {
  renderedPixels = []
  pendingBlobs.length = 0
  vi.mocked(raster.loadRaster).mockReset().mockResolvedValue(loaded())
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(
    () =>
      ({
        createImageData: (w: number, h: number) => ({
          width: w,
          height: h,
          data: new Uint8ClampedArray(w * h * 4),
        }),
        putImageData: (data: ImageData) =>
          renderedPixels.push(data.data.slice()),
      }) as unknown as CanvasRenderingContext2D,
  )
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(
    function (callback) {
      pendingBlobs.push(callback)
    },
  )
  vi.stubGlobal('PointerEvent', MouseEvent)
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

it('starts empty, supports keyboard mask upload, and exports binary pixels rather than red overlay', async () => {
  const onChange = vi.fn()
  render(<MaskEditor file={original()} onChange={onChange} />)
  await screen.findByText('2 × 1 像素')
  expect(onChange).toHaveBeenLastCalledWith(null)
  vi.mocked(raster.loadRaster).mockResolvedValueOnce(
    loaded(2, 1, new Uint8ClampedArray([255, 255, 255, 255, 0, 0, 0, 255])),
  )
  fireEvent.change(screen.getByLabelText('上传黑白遮罩'), {
    target: { files: [original()] },
  })
  await waitFor(() => expect(pendingBlobs).toHaveLength(1))
  expect([...renderedPixels.at(-1)!]).toEqual([
    255, 255, 255, 255, 0, 0, 0, 255,
  ])
  const blob = new Blob(['png'], { type: 'image/png' })
  await act(async () => pendingBlobs.shift()!(blob))
  expect(onChange).toHaveBeenLastCalledWith(blob)
  fireEvent.click(screen.getByRole('button', { name: '清空遮罩' }))
  expect(onChange).toHaveBeenLastCalledWith(null)
  expect(screen.getByRole('button', { name: '撤销' })).toBeEnabled()
})

it('invalidates the old mask at pointer down and ignores late export callbacks after clear', async () => {
  const onChange = vi.fn()
  render(<MaskEditor file={original()} onChange={onChange} />)
  const canvas = await screen.findByLabelText('遮罩绘制画布')
  vi.spyOn(canvas, 'getBoundingClientRect').mockReturnValue({
    left: 0,
    top: 0,
    width: 200,
    height: 100,
  } as DOMRect)
  fireEvent.pointerDown(canvas, {
    clientX: 50,
    clientY: 50,
    button: 0,
    pointerId: 1,
  })
  expect(onChange).toHaveBeenLastCalledWith(null)
  fireEvent.pointerUp(canvas, { clientX: 50, clientY: 50, pointerId: 1 })
  expect(pendingBlobs).toHaveLength(1)
  fireEvent.click(screen.getByRole('button', { name: '清空遮罩' }))
  act(() => pendingBlobs[0](new Blob(['stale'], { type: 'image/png' })))
  expect(onChange.mock.calls.every(([value]) => value === null)).toBe(true)
})

it('ignores a previous file decode and export after changing original', async () => {
  let resolveOld!: (value: ReturnType<typeof loaded>) => void
  vi.mocked(raster.loadRaster).mockReturnValueOnce(
    new Promise((resolve) => {
      resolveOld = resolve
    }),
  )
  const onChange = vi.fn()
  const view = render(<MaskEditor file={original()} onChange={onChange} />)
  view.rerender(<MaskEditor file={original()} onChange={onChange} />)
  await screen.findByText('2 × 1 像素')
  await act(async () =>
    resolveOld(loaded(3, 1, new Uint8ClampedArray(12).fill(255))),
  )
  expect(screen.queryByText('3 × 1 像素')).not.toBeInTheDocument()
  expect(onChange).toHaveBeenLastCalledWith(null)
})

it('reports invalid originals and keeps mask unavailable', async () => {
  vi.mocked(raster.loadRaster).mockResolvedValueOnce(
    loaded(1, 1, new Uint8ClampedArray([0, 0, 0, 0])),
  )
  const onChange = vi.fn()
  render(<MaskEditor file={original()} onChange={onChange} />)
  expect(await screen.findByRole('alert')).toHaveTextContent('不透明')
  expect(onChange).toHaveBeenLastCalledWith(null)
  expect(screen.queryByLabelText('遮罩绘制画布')).not.toBeInTheDocument()
})

it('rejects a wrongly sized uploaded mask without publishing it', async () => {
  const onChange = vi.fn()
  render(<MaskEditor file={original()} onChange={onChange} />)
  await screen.findByText('2 × 1 像素')
  vi.mocked(raster.loadRaster).mockResolvedValueOnce(loaded(1, 2))
  fireEvent.change(screen.getByLabelText('上传黑白遮罩'), {
    target: { files: [original()] },
  })
  expect(await screen.findByRole('alert')).toHaveTextContent('尺寸')
  expect(pendingBlobs).toHaveLength(0)
  expect(onChange).toHaveBeenLastCalledWith(null)
})

it('disables all editing controls when the caller is submitting', async () => {
  render(<MaskEditor file={original()} onChange={vi.fn()} disabled />)
  await screen.findByText('2 × 1 像素')
  expect(screen.getByLabelText('上传黑白遮罩')).toBeDisabled()
  expect(screen.getByRole('button', { name: '画笔' })).toBeDisabled()
})

it('cannot publish an exported mask after unmount, even when PNG encoding completes later', async () => {
  const onChange = vi.fn()
  const view = render(<MaskEditor file={original()} onChange={onChange} />)
  await screen.findByText('2 × 1 像素')
  vi.mocked(raster.loadRaster).mockResolvedValueOnce(
    loaded(2, 1, new Uint8ClampedArray(8).fill(255)),
  )
  fireEvent.change(screen.getByLabelText('上传黑白遮罩'), {
    target: { files: [original()] },
  })
  await waitFor(() => expect(pendingBlobs).toHaveLength(1))
  view.unmount()
  await act(async () =>
    pendingBlobs[0](new Blob(['stale'], { type: 'image/png' })),
  )
  expect(onChange.mock.calls.every(([value]) => value === null)).toBe(true)
})

it('reports a failed PNG export and leaves no submittable old mask', async () => {
  const onChange = vi.fn()
  render(<MaskEditor file={original()} onChange={onChange} />)
  await screen.findByText('2 × 1 像素')
  vi.mocked(raster.loadRaster).mockResolvedValueOnce(
    loaded(2, 1, new Uint8ClampedArray(8).fill(255)),
  )
  fireEvent.change(screen.getByLabelText('上传黑白遮罩'), {
    target: { files: [original()] },
  })
  await waitFor(() => expect(pendingBlobs).toHaveLength(1))
  await act(async () => pendingBlobs[0](null))
  expect(await screen.findByRole('alert')).toHaveTextContent('导出失败')
  expect(onChange).toHaveBeenLastCalledWith(null)
})

it('draws at original coordinates on a CSS-scaled high-DPI canvas and ends on pointer cancel', async () => {
  vi.stubGlobal('devicePixelRatio', 3)
  vi.mocked(raster.loadRaster).mockResolvedValueOnce(
    loaded(20, 10, new Uint8ClampedArray(20 * 10 * 4).fill(255)),
  )
  render(<MaskEditor file={original()} onChange={vi.fn()} />)
  const canvas = (await screen.findByLabelText(
    '遮罩绘制画布',
  )) as HTMLCanvasElement
  expect([canvas.width, canvas.height]).toEqual([20, 10])
  vi.spyOn(canvas, 'getBoundingClientRect').mockReturnValue({
    left: 10,
    top: 20,
    width: 200,
    height: 100,
  } as DOMRect)
  fireEvent.change(screen.getByRole('slider', { name: '画笔大小' }), {
    target: { value: '2' },
  })
  fireEvent.pointerDown(canvas, {
    clientX: 115,
    clientY: 75,
    button: 0,
    pointerId: 1,
  })
  fireEvent.pointerCancel(canvas, { pointerId: 1 })
  expect(pendingBlobs).toHaveLength(1)
  const exported = renderedPixels.at(-1)!
  expect(exported).toHaveLength(20 * 10 * 4)
  expect([...exported.slice((5 * 20 + 10) * 4, (5 * 20 + 10) * 4 + 4)]).toEqual(
    [255, 255, 255, 255],
  )
  expect([...exported.slice(0, 4)]).toEqual([0, 0, 0, 255])
  const rendered = renderedPixels.length
  fireEvent.pointerMove(canvas, { clientX: 10, clientY: 20, pointerId: 1 })
  expect(renderedPixels).toHaveLength(rendered)
})

it('keeps editing disabled when the browser cannot create a canvas context', async () => {
  vi.mocked(HTMLCanvasElement.prototype.getContext).mockReturnValue(null)
  const onChange = vi.fn()
  render(<MaskEditor file={original()} onChange={onChange} />)
  expect(await screen.findByRole('alert')).toHaveTextContent('画布')
  expect(screen.getByRole('button', { name: '画笔' })).toBeDisabled()
  expect(screen.queryByLabelText('遮罩绘制画布')).not.toBeInTheDocument()
  expect(onChange).toHaveBeenLastCalledWith(null)
})
