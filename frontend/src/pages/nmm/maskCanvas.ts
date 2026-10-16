/** Original-pixel geometry and binary data. Display scaling never changes this buffer. */
export type Point = { x: number; y: number }
export type Bounds = { x: number; y: number; width: number; height: number }
export type Raster = { width: number; height: number; data: Uint8ClampedArray }
const MAX_PIXELS = 4_000_000
const MAX_BYTES = 20 * 1024 * 1024

export function imagePoint(
  clientX: number,
  clientY: number,
  rect: { left: number; top: number; width: number; height: number },
  width: number,
  height: number,
): Point {
  return {
    x: Math.max(
      0,
      Math.min(width - 1, ((clientX - rect.left) / rect.width) * width),
    ),
    y: Math.max(
      0,
      Math.min(height - 1, ((clientY - rect.top) / rect.height) * height),
    ),
  }
}

function validateSize(width: number, height: number, bytes: number) {
  if (bytes > MAX_BYTES || bytes <= 0)
    throw new Error('图片不能为空，且不得超过 20 MiB。')
  if (
    !Number.isInteger(width) ||
    !Number.isInteger(height) ||
    width < 1 ||
    height < 1 ||
    width * height > MAX_PIXELS
  ) {
    throw new Error('原图与遮罩最多 400 万像素。')
  }
}

export function validateOriginal(
  width: number,
  height: number,
  bytes: number,
  data: Uint8ClampedArray,
) {
  validateSize(width, height, bytes)
  if (data.length !== width * height * 4)
    throw new Error('图片像素数据不完整。')
  for (let i = 3; i < data.length; i += 4) {
    if (data[i] !== 255) throw new Error('原图必须完全不透明，请先合成背景。')
  }
}

export function readBinaryMask(
  data: Uint8ClampedArray,
  width: number,
  height: number,
  expectedWidth: number,
  expectedHeight: number,
) {
  if (
    width !== expectedWidth ||
    height !== expectedHeight ||
    data.length !== width * height * 4
  ) {
    throw new Error('遮罩尺寸必须与原图一致。')
  }
  const pixels = new Uint8Array(width * height)
  for (let i = 0; i < pixels.length; i++) {
    const p = i * 4
    if (
      (data[p] !== 0 && data[p] !== 255) ||
      data[p + 1] !== data[p] ||
      data[p + 2] !== data[p] ||
      data[p + 3] !== 255
    ) {
      throw new Error('遮罩必须为不透明的纯黑白图片，不接受灰色或透明像素。')
    }
    pixels[i] = data[p] === 255 ? 1 : 0
  }
  return pixels
}

export class BinaryMask {
  readonly width: number
  readonly height: number
  pixels: Uint8Array
  private history: Uint8Array[] = []
  private before: Uint8Array | null = null
  private changed = false
  private limit: number

  constructor(width: number, height: number, historyLimit = 12) {
    validateSize(width, height, 1)
    this.width = width
    this.height = height
    this.pixels = new Uint8Array(width * height)
    this.limit = Math.max(1, Math.min(12, historyLimit))
  }

  get hasSelection() {
    return this.pixels.includes(1)
  }
  get canUndo() {
    return this.history.length > 0
  }
  begin() {
    this.before = this.pixels.slice()
    this.changed = false
  }

  paint(from: Point, to: Point, radius: number, erase: boolean): Bounds {
    const x0 = Math.max(0, Math.floor(Math.min(from.x, to.x) - radius))
    const y0 = Math.max(0, Math.floor(Math.min(from.y, to.y) - radius))
    const x1 = Math.min(this.width, Math.ceil(Math.max(from.x, to.x) + radius))
    const y1 = Math.min(this.height, Math.ceil(Math.max(from.y, to.y) + radius))
    const dx = to.x - from.x,
      dy = to.y - from.y,
      length = dx * dx + dy * dy
    for (let y = y0; y < y1; y++) {
      for (let x = x0; x < x1; x++) {
        const t =
          length === 0
            ? 0
            : Math.max(
                0,
                Math.min(
                  1,
                  ((x + 0.5 - from.x) * dx + (y + 0.5 - from.y) * dy) / length,
                ),
              )
        if (
          (x + 0.5 - from.x - t * dx) ** 2 + (y + 0.5 - from.y - t * dy) ** 2 <=
          radius * radius
        ) {
          const index = y * this.width + x,
            value = erase ? 0 : 1
          if (this.pixels[index] !== value) {
            this.pixels[index] = value
            this.changed = true
          }
        }
      }
    }
    return { x: x0, y: y0, width: x1 - x0, height: y1 - y0 }
  }

  end() {
    if (this.before && this.changed) {
      this.history.push(this.before)
      if (this.history.length > this.limit) this.history.shift()
    }
    this.before = null
    this.changed = false
  }

  replace(pixels: Uint8Array) {
    if (
      pixels.length !== this.pixels.length ||
      pixels.some((p) => p !== 0 && p !== 1)
    )
      throw new Error('遮罩像素无效。')
    this.begin()
    this.changed = this.pixels.some((p, i) => p !== pixels[i])
    this.pixels = pixels.slice()
    this.end()
  }

  clear() {
    this.replace(new Uint8Array(this.pixels.length))
  }
  undo() {
    const previous = this.history.pop()
    if (previous) this.pixels = previous
  }

  rgba() {
    const data = new Uint8ClampedArray(this.pixels.length * 4)
    for (let i = 0; i < this.pixels.length; i++) {
      const p = i * 4,
        value = this.pixels[i] * 255
      data[p] = data[p + 1] = data[p + 2] = value
      data[p + 3] = 255
    }
    return data
  }
}

/** Native decoder owns/revokes its object URL on success, failure and cancellation. */
export function loadRaster(file: File, signal: AbortSignal): Promise<Raster> {
  if (file.size <= 0 || file.size > MAX_BYTES)
    return Promise.reject(new Error('图片不能为空，且不得超过 20 MiB。'))
  if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type))
    return Promise.reject(new Error('请选择 PNG、JPEG 或 WebP 图片。'))
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException('Aborted', 'AbortError'))
      return
    }
    const image = new Image()
    const url = URL.createObjectURL(file)
    const release = () => {
      image.onload = image.onerror = null
      signal.removeEventListener('abort', abort)
      URL.revokeObjectURL(url)
    }
    const abort = () => {
      release()
      image.src = ''
      reject(new DOMException('Aborted', 'AbortError'))
    }
    signal.addEventListener('abort', abort, { once: true })
    image.onerror = () => {
      release()
      reject(
        new Error('无法读取这张图片，请使用完整的 PNG、JPEG 或 WebP 文件。'),
      )
    }
    image.onload = () => {
      try {
        const width = image.naturalWidth,
          height = image.naturalHeight
        validateSize(width, height, file.size)
        const canvas = document.createElement('canvas')
        canvas.width = width
        canvas.height = height
        const context = canvas.getContext('2d', { willReadFrequently: true })
        if (!context) throw new Error('浏览器无法读取图片像素。')
        context.drawImage(image, 0, 0)
        const data = context.getImageData(0, 0, width, height).data
        resolve({ width, height, data })
      } catch (error) {
        reject(error)
      } finally {
        release()
      }
    }
    image.src = url
  })
}

export function putRaster(canvas: HTMLCanvasElement, raster: Raster) {
  canvas.width = raster.width
  canvas.height = raster.height
  const context = canvas.getContext('2d')
  if (!context) throw new Error('浏览器无法使用画布，请尝试其他浏览器。')
  const image = context.createImageData(raster.width, raster.height)
  image.data.set(raster.data)
  context.putImageData(image, 0, 0)
}

export function paintOverlay(
  canvas: HTMLCanvasElement,
  mask: BinaryMask,
  bounds?: Bounds,
) {
  const box = bounds ?? { x: 0, y: 0, width: mask.width, height: mask.height }
  if (!box.width || !box.height) return
  const context = canvas.getContext('2d')
  if (!context) throw new Error('浏览器无法使用画布。')
  const image = context.createImageData(box.width, box.height)
  for (let y = 0; y < box.height; y++) {
    for (let x = 0; x < box.width; x++) {
      const p = (y * box.width + x) * 4
      image.data[p] = 255 // Semantic edit-region red; never part of the exported mask.
      image.data[p + 3] =
        mask.pixels[(y + box.y) * mask.width + x + box.x] * 255
    }
  }
  context.putImageData(image, box.x, box.y)
}

export async function exportMask(mask: BinaryMask): Promise<Blob> {
  const canvas = document.createElement('canvas')
  putRaster(canvas, {
    width: mask.width,
    height: mask.height,
    data: mask.rgba(),
  })
  return new Promise((resolve, reject) =>
    canvas.toBlob((blob) => {
      if (blob && blob.type === 'image/png') resolve(blob)
      else reject(new Error('遮罩导出失败，请重新编辑后再试。'))
    }, 'image/png'),
  )
}
