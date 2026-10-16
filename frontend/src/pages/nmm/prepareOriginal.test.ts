import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { prepareOriginal, workingDimensions } from './prepareOriginal'

const source = () =>
  new File(
    [new Uint8Array([255, 216, 255, 224, 0, 2, 255, 217])],
    'armor.jpg',
    { type: 'image/jpeg' },
  )
let bitmap: { width: number; height: number; close: ReturnType<typeof vi.fn> }
let transparent: boolean
let context: CanvasRenderingContext2D
let outputSize: [number, number] | undefined
beforeEach(() => {
  transparent = false
  outputSize = undefined
  bitmap = { width: 4000, height: 3000, close: vi.fn() }
  vi.stubGlobal(
    'createImageBitmap',
    vi.fn(async () => bitmap),
  )
  context = {
    clearRect: vi.fn(),
    drawImage: vi.fn(),
    getImageData: (_x: number, _y: number, w: number, h: number) => {
      const data = new Uint8ClampedArray(w * h * 4)
      for (let i = 3; i < data.length; i += 4) data[i] = 255
      if (transparent) data[data.length - 1] = 254
      return { data }
    },
  } as unknown as CanvasRenderingContext2D
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(context)
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(function (
    this: HTMLCanvasElement,
    callback,
  ) {
    outputSize = [this.width, this.height]
    callback(new Blob(['encoded PNG'], { type: 'image/png' }))
  })
})
afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

it('floors proportional working dimensions without enlarging or exceeding four megapixels', () => {
  expect(workingDimensions(4000, 3000)).toEqual({ width: 2309, height: 1732 })
  expect(workingDimensions(3000, 4000)).toEqual({ width: 1732, height: 2309 })
  expect(workingDimensions(1600, 1200)).toEqual({ width: 1600, height: 1200 })
  for (const [w, h] of [
    [20000000, 1],
    [1, 20000000],
    [4472, 4472],
    [4001, 1000],
  ]) {
    const result = workingDimensions(w, h)
    expect(result.width * result.height).toBeLessThanOrEqual(4_000_000)
    expect(result.width).toBeLessThanOrEqual(w)
    expect(result.height).toBeLessThanOrEqual(h)
    expect(Math.min(result.width, result.height)).toBeGreaterThan(0)
  }
})
it('prepares a twelve-megapixel source once as a bounded opaque PNG working file', async () => {
  const file = source()
  const result = await prepareOriginal(file, new AbortController().signal)
  expect(result).toMatchObject({
    sourceName: 'armor.jpg',
    sourceWidth: 4000,
    sourceHeight: 3000,
    width: 2309,
    height: 1732,
    resized: true,
  })
  expect(result.file).not.toBe(file)
  expect(result.file.type).toBe('image/png')
  expect(result.file.name).toBe('armor-work.png')
  expect(outputSize).toEqual([2309, 1732])
  expect(createImageBitmap).toHaveBeenCalledTimes(1)
  expect(bitmap.close).toHaveBeenCalledTimes(1)
})
it('keeps a small original File and its bytes instead of re-encoding', async () => {
  bitmap.width = 1600
  bitmap.height = 1200
  const file = source()
  const result = await prepareOriginal(file, new AbortController().signal)
  expect(result.file).toBe(file)
  expect(result.resized).toBe(false)
  expect(outputSize).toBeUndefined()
  expect(bitmap.close).toHaveBeenCalledTimes(1)
})
it('rejects sources above twenty megapixels before allocating a canvas', async () => {
  bitmap.width = 6000
  bitmap.height = 4000
  await expect(
    prepareOriginal(source(), new AbortController().signal),
  ).rejects.toThrow(/2000 万像素/)
  expect(HTMLCanvasElement.prototype.getContext).not.toHaveBeenCalled()
  expect(bitmap.close).toHaveBeenCalledTimes(1)
})
it('rejects transparent source pixels instead of hiding them through resizing', async () => {
  transparent = true
  await expect(
    prepareOriginal(source(), new AbortController().signal),
  ).rejects.toThrow(/不透明/)
  expect(outputSize).toBeUndefined()
  expect(bitmap.close).toHaveBeenCalledTimes(1)
})
it('rejects oversized bytes and unsupported types before decoding', async () => {
  const huge = source()
  Object.defineProperty(huge, 'size', { value: 20 * 1024 * 1024 + 1 })
  await expect(
    prepareOriginal(huge, new AbortController().signal),
  ).rejects.toThrow(/20 MiB/)
  await expect(
    prepareOriginal(
      new File(['data'], 'phone.heic', { type: 'image/heic' }),
      new AbortController().signal,
    ),
  ).rejects.toThrow(/PNG.*JPEG.*WebP/)
  expect(createImageBitmap).not.toHaveBeenCalled()
})
it('closes a decoder arriving after cancellation without making a working file', async () => {
  let resolve!: (value: ImageBitmap) => void
  vi.mocked(createImageBitmap).mockReturnValue(
    new Promise((done) => {
      resolve = done
    }),
  )
  const controller = new AbortController()
  const result = prepareOriginal(source(), controller.signal)
  await vi.waitFor(() => expect(createImageBitmap).toHaveBeenCalledTimes(1))
  controller.abort()
  resolve(bitmap as unknown as ImageBitmap)
  await expect(result).rejects.toMatchObject({ name: 'AbortError' })
  expect(bitmap.close).toHaveBeenCalledTimes(1)
  expect(outputSize).toBeUndefined()
})
it('rejects canceled encoding and an unavailable encoder with explicit failures', async () => {
  let complete!: BlobCallback
  vi.mocked(HTMLCanvasElement.prototype.toBlob).mockImplementation(
    (callback) => {
      complete = callback
    },
  )
  const controller = new AbortController()
  const result = prepareOriginal(source(), controller.signal)
  const outcome = result.then(
    () => null,
    (error) => error,
  )
  await vi.waitFor(() => expect(complete).toBeTypeOf('function'))
  controller.abort()
  complete(new Blob(['png'], { type: 'image/png' }))
  expect(await outcome).toMatchObject({ name: 'AbortError' })
  expect(bitmap.close).toHaveBeenCalledTimes(1)
  vi.mocked(HTMLCanvasElement.prototype.toBlob).mockImplementation((callback) =>
    callback(null),
  )
  await expect(
    prepareOriginal(source(), new AbortController().signal),
  ).rejects.toThrow(/工作图/)
})

function ascii(value: string) {
  return new Uint8Array([...value].map((char) => char.charCodeAt(0)))
}
function chunk(type: string, payload: Uint8Array, png: boolean) {
  const result = new Uint8Array(
    payload.length + (png ? 12 : 8) + (!png ? payload.length % 2 : 0),
  )
  const view = new DataView(result.buffer)
  view.setUint32(png ? 0 : 4, payload.length, !png)
  result.set(ascii(type), png ? 4 : 0)
  result.set(payload, 8)
  return result
}
function png(...chunks: Uint8Array[]) {
  return new File(
    [
      new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]),
      ...chunks,
      chunk('IEND', new Uint8Array(), true),
    ],
    'source.png',
    { type: 'image/png' },
  )
}
function webp(...chunks: Uint8Array[]) {
  const header = new Uint8Array(12)
  header.set(ascii('RIFF'))
  header.set(ascii('WEBP'), 8)
  new DataView(header.buffer).setUint32(
    4,
    4 + chunks.reduce((total, part) => total + part.length, 0),
    true,
  )
  return new File([header, ...chunks], 'source.webp', { type: 'image/webp' })
}
it('rejects a large APNG instead of flattening its first frame to PNG', async () => {
  const file = png(chunk('acTL', new Uint8Array(8), true))
  await expect(
    prepareOriginal(file, new AbortController().signal),
  ).rejects.toThrow(/动图/)
  expect(createImageBitmap).not.toHaveBeenCalled()
})
it.each(['ANIM', 'ANMF', 'VP8X'])(
  'rejects large animated WebP using the %s chunk',
  async (type) => {
    const data = new Uint8Array(type === 'VP8X' ? 10 : 6)
    if (type === 'VP8X') data[0] = 2
    await expect(
      prepareOriginal(
        webp(chunk(type, data, false)),
        new AbortController().signal,
      ),
    ).rejects.toThrow(/动图/)
    expect(createImageBitmap).not.toHaveBeenCalled()
  },
)
it('does not mistake animation words in static metadata for animation chunks', async () => {
  const a = await prepareOriginal(
    png(chunk('tEXt', ascii('description\0acTL fcTL fdAT'), true)),
    new AbortController().signal,
  )
  const b = await prepareOriginal(
    webp(
      chunk('EXIF', ascii('ANIM ANMF'), false),
      chunk('VP8 ', new Uint8Array(2), false),
    ),
    new AbortController().signal,
  )
  expect(a.resized).toBe(true)
  expect(b.resized).toBe(true)
  expect(createImageBitmap).toHaveBeenCalledTimes(2)
})
it('rejects PNG chunk lengths and WebP RIFF bounds that extend beyond the file', async () => {
  const malformed = chunk('tEXt', new Uint8Array(1), true)
  new DataView(malformed.buffer).setUint32(0, 0x7fffffff)
  await expect(
    prepareOriginal(png(malformed), new AbortController().signal),
  ).rejects.toThrow(/结构|完整/)
  const bad = new Uint8Array(20)
  bad.set(ascii('RIFF'))
  bad.set(ascii('WEBP'), 8)
  bad.set(ascii('EXIF'), 12)
  new DataView(bad.buffer).setUint32(4, 12, true)
  new DataView(bad.buffer).setUint32(16, 12, true)
  await expect(
    prepareOriginal(
      new File([bad], 'bad.webp', { type: 'image/webp' }),
      new AbortController().signal,
    ),
  ).rejects.toThrow(/结构|完整/)
  expect(createImageBitmap).not.toHaveBeenCalled()
})
