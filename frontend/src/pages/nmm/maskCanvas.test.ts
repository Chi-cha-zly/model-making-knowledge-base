import { describe, expect, it, vi } from 'vitest'
import {
  BinaryMask,
  imagePoint,
  validateOriginal,
  readBinaryMask,
  loadRaster,
  exportMask,
} from './maskCanvas'

describe('original-pixel mask', () => {
  it('maps a CSS-scaled canvas to original pixels without multiplying devicePixelRatio', () => {
    const rect = { left: 10, top: 20, width: 200, height: 100 }
    expect(imagePoint(110, 45, rect, 800, 400)).toEqual({ x: 400, y: 100 })
    expect(imagePoint(-50, 500, rect, 800, 400)).toEqual({ x: 0, y: 399 })
  })

  it('paints continuous strokes, exports opaque binary pixels, and preserves the outside', () => {
    const mask = new BinaryMask(10, 6)
    mask.begin()
    mask.paint({ x: 2.5, y: 2.5 }, { x: 7.5, y: 2.5 }, 1, false)
    mask.end()
    const rgba = mask.rgba()
    expect([mask.width, mask.height, rgba.length]).toEqual([10, 6, 240])
    expect([...rgba.slice((2 * 10 + 4) * 4, (2 * 10 + 4) * 4 + 4)]).toEqual([
      255, 255, 255, 255,
    ])
    expect([...rgba.slice(0, 4)]).toEqual([0, 0, 0, 255])
    expect(new Set(rgba)).toEqual(new Set([0, 255]))
    for (let i = 3; i < rgba.length; i += 4) expect(rgba[i]).toBe(255)
  })

  it('erases pixels and undoes whole strokes, including clear', () => {
    const mask = new BinaryMask(5, 5)
    mask.begin()
    mask.paint({ x: 2.5, y: 2.5 }, { x: 2.5, y: 2.5 }, 2, false)
    mask.end()
    const painted = mask.rgba()
    mask.begin()
    mask.paint({ x: 2.5, y: 2.5 }, { x: 2.5, y: 2.5 }, 1, true)
    mask.end()
    expect(mask.rgba()[(2 * 5 + 2) * 4]).toBe(0)
    mask.undo()
    expect(mask.rgba()).toEqual(painted)
    mask.clear()
    expect(mask.hasSelection).toBe(false)
    mask.undo()
    expect(mask.rgba()).toEqual(painted)
  })

  it('bounds history and does not add a no-op stroke', () => {
    const mask = new BinaryMask(5, 5, 2)
    for (const x of [0.5, 2.5, 4.5]) {
      mask.begin()
      mask.paint({ x, y: 0.5 }, { x, y: 0.5 }, 0.5, false)
      mask.end()
    }
    mask.begin()
    mask.paint({ x: 4.5, y: 0.5 }, { x: 4.5, y: 0.5 }, 0.5, false)
    mask.end()
    mask.undo()
    mask.undo()
    expect(mask.canUndo).toBe(false)
    expect(mask.rgba()[0]).toBe(255)
    expect(mask.rgba()[8]).toBe(0)
  })

  it('validates dimensions, byte limit and opacity before using the original', () => {
    expect(() =>
      validateOriginal(2001, 2000, 10, new Uint8ClampedArray()),
    ).toThrow(/400/)
    expect(() =>
      validateOriginal(
        1,
        1,
        21 * 1024 * 1024,
        new Uint8ClampedArray([0, 0, 0, 255]),
      ),
    ).toThrow(/20/)
    expect(() =>
      validateOriginal(1, 1, 10, new Uint8ClampedArray([0, 0, 0, 254])),
    ).toThrow(/不透明/)
    expect(() =>
      validateOriginal(1, 1, 10, new Uint8ClampedArray([1, 2, 3, 255])),
    ).not.toThrow()
  })

  it('imports only same-size opaque black/white masks and preserves exact selections', () => {
    const pixels = new Uint8ClampedArray([0, 0, 0, 255, 255, 255, 255, 255])
    expect([...readBinaryMask(pixels, 2, 1, 2, 1)]).toEqual([0, 1])
    expect(() => readBinaryMask(pixels, 2, 1, 1, 2)).toThrow(/尺寸/)
    expect(() =>
      readBinaryMask(new Uint8ClampedArray([100, 100, 100, 255]), 1, 1, 1, 1),
    ).toThrow(/黑白/)
    expect(() =>
      readBinaryMask(new Uint8ClampedArray([255, 255, 255, 0]), 1, 1, 1, 1),
    ).toThrow(/黑白/)
  })
})

it('cancels native image loading and revokes the object URL without using stale pixels', async () => {
  const revoke = vi.fn()
  vi.stubGlobal('URL', {
    createObjectURL: () => 'blob:local-image',
    revokeObjectURL: revoke,
  })
  const images: Array<{
    onload: (() => void) | null
    onerror: (() => void) | null
    src: string
  }> = []
  vi.stubGlobal(
    'Image',
    class {
      onload = null
      onerror = null
      src = ''
      constructor() {
        images.push(this)
      }
    },
  )
  try {
    const controller = new AbortController()
    const result = loadRaster(
      new File(['image'], 'source.png', { type: 'image/png' }),
      controller.signal,
    )
    controller.abort()
    await expect(result).rejects.toMatchObject({ name: 'AbortError' })
    expect(images[0].onload).toBeNull()
    expect(images[0].src).toBe('')
    expect(revoke).toHaveBeenCalledExactlyOnceWith('blob:local-image')
  } finally {
    vi.unstubAllGlobals()
  }
})

it('returns an export error as a rejected promise when a canvas is unavailable', async () => {
  const spy = vi
    .spyOn(HTMLCanvasElement.prototype, 'getContext')
    .mockReturnValue(null)
  try {
    await expect(exportMask(new BinaryMask(2, 1))).rejects.toThrow(/画布/)
  } finally {
    spy.mockRestore()
  }
})
