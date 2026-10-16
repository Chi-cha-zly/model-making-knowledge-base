import { afterEach, describe, expect, it, vi } from 'vitest'
import { assetUrl, eventsUrl, exportUrl, NmmApiError, nmmApi } from './nmmApi'

const params = {
  metal: 'silver',
  light_direction: 'top_left' as const,
  contrast: 'high' as const,
  step_count: 7,
  notes: '',
}
afterEach(() => vi.unstubAllGlobals())

describe('NMM HTTP protocol', () => {
  it('sends canonical multipart fields without overriding browser boundary', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify({ id: 'project' })))
    vi.stubGlobal('fetch', fetcher)
    const original = new File(['source'], 'original.png', { type: 'image/png' })
    const reference = new File(['ref'], 'ref.png', { type: 'image/png' })
    const mask = new Blob(['binary-mask'], { type: 'image/png' })
    await nmmApi.create({
      title: 'shield',
      params,
      original,
      mask,
      references: [{ file: reference, role: 'reference_steps' }],
    })
    const [url, init] = fetcher.mock.calls[0]
    expect(url).toBe('/api/v1/nmm/projects')
    expect(init.headers).toBeUndefined()
    const body = init.body as FormData
    expect(body.get('title')).toBe('shield')
    expect(JSON.parse(body.get('params') as string)).toEqual(params)
    expect(body.get('original')).toBe(original)
    expect(body.getAll('roles')).toEqual(['reference_steps'])
    expect(body.getAll('references')).toEqual([reference])
    expect((body.get('mask') as File).name).toBe('mask.png')
  })

  it('handles an empty delete response', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response(null, { status: 204 })),
    )
    await expect(nmmApi.remove('p')).resolves.toBeUndefined()
  })

  it('keeps safe JSON error detail/status and never retries mutations', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: '项目版本已改变。' }), {
        status: 409,
      }),
    )
    vi.stubGlobal('fetch', fetcher)
    await expect(
      nmmApi.run('p', {
        request_id: 'stable',
        revision: 1,
        confirm_generation: true,
      }),
    ).rejects.toMatchObject({ status: 409, message: '项目版本已改变。' })
    expect(fetcher).toHaveBeenCalledTimes(1)
  })

  it('uses GET current estimate and POST proposed params with encoded paths', async () => {
    const fetcher = vi
      .fn()
      .mockImplementation(() => Promise.resolve(new Response('{}')))
    vi.stubGlobal('fetch', fetcher)
    await nmmApi.estimate('id /?')
    await nmmApi.estimate('id /?', params)
    expect(fetcher.mock.calls[0][0]).toBe(
      '/api/v1/nmm/projects/id%20%2F%3F/estimate',
    )
    expect(fetcher.mock.calls[1][1].method).toBe('POST')
    expect(JSON.parse(fetcher.mock.calls[1][1].body)).toEqual({ params })
  })

  it('does not surface non-JSON server bodies', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(
          new Response('/private/path secret', { status: 500 }),
        ),
    )
    await expect(nmmApi.detail('p')).rejects.toEqual(
      new NmmApiError(500, '请求失败（500）'),
    )
  })

  it('encodes every generated media/event/export path', () => {
    expect(assetUrl('a/b')).toBe('/api/v1/nmm/assets/a%2Fb')
    expect(eventsUrl('p?')).toBe('/api/v1/nmm/projects/p%3F/events')
    expect(exportUrl('p#')).toBe('/api/v1/nmm/projects/p%23/export')
  })
})

it('treats a successful response with unreadable JSON as an unknown operation result', async () => {
  const fetcher = vi
    .fn()
    .mockResolvedValue(new Response('not-json', { status: 200 }))
  vi.stubGlobal('fetch', fetcher)
  await expect(
    nmmApi.run('p', {
      request_id: 'keep-this-id',
      revision: 1,
      confirm_generation: true,
    }),
  ).rejects.toMatchObject({
    status: 0,
    message: '响应未能完整读取，请使用相同操作重试以核对结果。',
  })
  expect(fetcher).toHaveBeenCalledTimes(1)
})

it('downloads ZIP bytes and preserves HTTP download failures without retry', async () => {
  const fetcher = vi
    .fn()
    .mockResolvedValueOnce(new Response('zip'))
    .mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: '成果包超过限制' }), {
        status: 413,
      }),
    )
  vi.stubGlobal('fetch', fetcher)
  expect((await nmmApi.downloadExport('p/')).size).toBe(3)
  expect(fetcher.mock.calls[0][0]).toBe('/api/v1/nmm/projects/p%2F/export')
  await expect(nmmApi.downloadExport('p')).rejects.toMatchObject({
    status: 413,
    message: '成果包超过限制',
  })
  expect(fetcher).toHaveBeenCalledTimes(2)
})

it('normalizes interrupted ZIP reads and network failures', async () => {
  vi.stubGlobal(
    'fetch',
    vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        blob: () => Promise.reject(new Error('private')),
      })
      .mockRejectedValueOnce(new Error('private')),
  )
  await expect(nmmApi.downloadExport('p')).rejects.toMatchObject({ status: 0 })
  await expect(nmmApi.downloadExport('p')).rejects.toMatchObject({ status: 0 })
})
