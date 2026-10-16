/** Prepare a local working image only; no upload, persistence, or provider access. */
export type PreparedOriginal = {
  file: File
  sourceName: string
  sourceWidth: number
  sourceHeight: number
  width: number
  height: number
  resized: boolean
}
const MAX_INPUT_PIXELS = 20_000_000
const MAX_WORK_PIXELS = 4_000_000
const MAX_BYTES = 20 * 1024 * 1024

export function workingDimensions(width: number, height: number) {
  if (
    !Number.isSafeInteger(width) ||
    !Number.isSafeInteger(height) ||
    width < 1 ||
    height < 1 ||
    width * height > MAX_INPUT_PIXELS
  )
    throw new Error('原图最多 2000 万像素，请先缩小图片后再选择。')
  const scale = Math.min(
    1,
    Math.sqrt(MAX_WORK_PIXELS / (width * height)),
    MAX_WORK_PIXELS / Math.max(width, height),
  )
  return {
    width: Math.max(1, Math.floor(width * scale)),
    height: Math.max(1, Math.floor(height * scale)),
  }
}
function checkCanceled(signal: AbortSignal) {
  if (signal.aborted) throw new DOMException('Aborted', 'AbortError')
}

/** Read at most the already-validated 20 MiB input, with cancelable local I/O. */
function readBytes(file: File, signal: AbortSignal): Promise<Uint8Array> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    const cleanup = () => {
      signal.removeEventListener('abort', abort)
      reader.onload = reader.onerror = reader.onabort = null
    }
    const abort = () => {
      cleanup()
      reader.abort()
      reject(new DOMException('Aborted', 'AbortError'))
    }
    reader.onload = () => {
      const result = reader.result
      cleanup()
      if (!(result instanceof ArrayBuffer))
        reject(new Error('无法完整读取图片。'))
      else resolve(new Uint8Array(result))
    }
    reader.onerror = () => {
      cleanup()
      reject(new Error('无法完整读取图片。'))
    }
    reader.onabort = abort
    signal.addEventListener('abort', abort, { once: true })
    if (signal.aborted) abort()
    else reader.readAsArrayBuffer(file)
  })
}

// Walk container records, never scan compressed pixels/metadata for words.
// PNG: https://www.w3.org/TR/png-3/#5Chunk-layout
// WebP: https://developers.google.com/speed/webp/docs/riff_container
async function rejectAnimation(file: File, signal: AbortSignal) {
  const bytes = await readBytes(file, signal)
  checkCanceled(signal)
  if (bytes.byteLength > MAX_BYTES) throw new Error('图片不得超过 20 MiB。')
  const invalid = () =>
    new Error('图片结构不完整或格式与内容不一致，请重新导出后再试。')
  const animated = () =>
    new Error('不支持动图，请选择静态 PNG、JPEG 或 WebP 图片。')
  const text = (offset: number) =>
    String.fromCharCode(...bytes.subarray(offset, offset + 4))
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  if (file.type === 'image/jpeg') {
    if (bytes[0] !== 255 || bytes[1] !== 216 || bytes[2] !== 255)
      throw invalid()
    return
  }
  if (file.type === 'image/png') {
    if (
      ![137, 80, 78, 71, 13, 10, 26, 10].every((value, i) => bytes[i] === value)
    )
      throw invalid()
    let offset = 8
    while (offset < bytes.length) {
      if (bytes.length - offset < 12) throw invalid()
      const size = view.getUint32(offset),
        type = text(offset + 4)
      const end = offset + 12 + size
      if (size > 0x7fffffff || end > bytes.length) throw invalid()
      if (['acTL', 'fcTL', 'fdAT'].includes(type)) throw animated()
      if (type === 'IEND') {
        if (size !== 0 || end !== bytes.length) throw invalid()
        return
      }
      offset = end
    }
    throw invalid()
  }
  if (bytes.length < 12 || text(0) !== 'RIFF' || text(8) !== 'WEBP')
    throw invalid()
  const limit = view.getUint32(4, true) + 8
  if (limit !== bytes.length) throw invalid()
  let offset = 12
  while (offset < limit) {
    if (limit - offset < 8) throw invalid()
    const type = text(offset),
      size = view.getUint32(offset + 4, true)
    const dataEnd = offset + 8 + size,
      end = dataEnd + (size % 2)
    if (end > limit || (size % 2 && bytes[dataEnd] !== 0)) throw invalid()
    if (type === 'ANIM' || type === 'ANMF') throw animated()
    if (type === 'VP8X') {
      if (size !== 10) throw invalid()
      if ((bytes[offset + 8] & 2) !== 0) throw animated()
    }
    offset = end
  }
}

export async function prepareOriginal(
  file: File,
  signal: AbortSignal,
): Promise<PreparedOriginal> {
  checkCanceled(signal)
  if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type))
    throw new Error(
      '仅支持 PNG、JPEG 或 WebP 图片；其他格式请先导出为这些格式。',
    )
  if (file.size <= 0 || file.size > MAX_BYTES)
    throw new Error('图片不能为空，且不得超过 20 MiB。')
  if (typeof createImageBitmap !== 'function')
    throw new Error('当前浏览器无法本地处理图片，请更新浏览器后重试。')
  await rejectAnimation(file, signal)
  checkCanceled(signal)
  let bitmap: ImageBitmap
  try {
    bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' })
  } catch {
    checkCanceled(signal)
    throw new Error('无法读取这张图片，请使用完整的 PNG、JPEG 或 WebP 文件。')
  }
  let canvas: HTMLCanvasElement | undefined
  try {
    checkCanceled(signal)
    const { width, height } = workingDimensions(bitmap.width, bitmap.height)
    const result = {
      file,
      sourceName: file.name,
      sourceWidth: bitmap.width,
      sourceHeight: bitmap.height,
      width,
      height,
      resized: width !== bitmap.width || height !== bitmap.height,
    }
    canvas = document.createElement('canvas')
    // Inspect the original alpha before resizing. Tiling avoids an additional
    // full-size 20 MP canvas/pixel buffer and never composites a background.
    canvas.width = Math.min(512, bitmap.width)
    canvas.height = Math.min(512, bitmap.height)
    const context = canvas.getContext('2d', { willReadFrequently: true })
    if (!context)
      throw new Error('浏览器无法读取图片像素，请更新浏览器后重试。')
    for (let y = 0; y < bitmap.height; y += 512) {
      for (let x = 0; x < bitmap.width; x += 512) {
        checkCanceled(signal)
        const w = Math.min(512, bitmap.width - x),
          h = Math.min(512, bitmap.height - y)
        context.clearRect(0, 0, canvas.width, canvas.height)
        context.drawImage(bitmap, x, y, w, h, 0, 0, w, h)
        const pixels = context.getImageData(0, 0, w, h).data
        for (let i = 3; i < pixels.length; i += 4) {
          if (pixels[i] !== 255)
            throw new Error(
              '原图必须完全不透明，请先合成背景。不会自动填充背景。',
            )
        }
      }
    }
    if (!result.resized) return result
    checkCanceled(signal)
    canvas.width = width
    canvas.height = height
    context.imageSmoothingEnabled = true
    context.imageSmoothingQuality = 'high'
    context.drawImage(bitmap, 0, 0, width, height)
    const blob = await new Promise<Blob>((resolve, reject) => {
      canvas!.toBlob((value) => {
        if (
          value &&
          value.type === 'image/png' &&
          value.size > 0 &&
          value.size <= MAX_BYTES
        )
          resolve(value)
        else reject(new Error('工作图转换失败，请先手动缩小图片后重试。'))
      }, 'image/png')
    })
    checkCanceled(signal)
    return {
      ...result,
      file: new File([blob], `${file.name.replace(/\.[^.]+$/, '')}-work.png`, {
        type: 'image/png',
        lastModified: file.lastModified,
      }),
    }
  } finally {
    bitmap.close()
    if (canvas) {
      canvas.width = 0
      canvas.height = 0
    }
  }
}
