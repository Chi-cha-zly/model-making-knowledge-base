const BASE = '/api/v1'

export type Source = { id: number; title: string; source_type?: string; url?: string | null; author?: string | null; license_note?: string | null; status?: string }
export type ContentBlock = { type: 'paragraph' | 'heading'; text: string } | { type: 'image'; attachment_id: number; caption: string }
export type Technique = { id: number; title: string; category: string; summary?: string | null; steps: string[]; content_blocks?: ContentBlock[]; source_ids: number[]; tags: string[]; status: string; visibility?: string; attachment_ids?: number[] }
export type Product = { id: number; brand: string; name: string; sku?: string | null; kind?: string | null; aliases: string[]; source_ids: number[]; status: string; visibility?: string; attachment_ids?: number[] }
export type SearchResult = { techniques: Technique[]; products: Product[]; ranking?: string[] }
export type RecordType = 'technique' | 'product'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, init)
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    const detail = body?.detail
    throw new Error(typeof detail === 'string' ? detail : `请求失败（${response.status}）`)
  }
  return response.json() as Promise<T>
}

const json = (body: unknown, method: string): RequestInit => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })

export const api = {
  sources: () => request<Source[]>('/sources'),
  techniques: () => request<Technique[]>('/techniques'),
  products: () => request<Product[]>('/products'),
  search: (query: string) => request<SearchResult>(`/search?q=${encodeURIComponent(query)}`),
  createSource: (body: Omit<Source, 'id'>) => request<Source>('/sources', json(body, 'POST')),
  updateSource: (id: number, body: Omit<Source, 'id'>) => request<Source>(`/sources/${id}`, json(body, 'PATCH')),
  withdrawSource: (id: number) => request<Source>(`/sources/${id}`, { method: 'DELETE' }),
  saveTechnique: (body: Omit<Technique, 'id'> & { original?: boolean }, id?: number) => request<Technique>(id ? `/techniques/${id}` : '/techniques', json(body, id ? 'PATCH' : 'POST')),
  withdrawTechnique: (id: number) => request<Technique>(`/techniques/${id}`, { method: 'DELETE' }),
  publishTechnique: (id: number) => request<Technique>(`/techniques/${id}`, json({ status: 'published' }, 'PATCH')),
  saveProduct: (body: Omit<Product, 'id'>, id?: number) => request<Product>(id ? `/products/${id}` : '/products', json(body, id ? 'PATCH' : 'POST')),
  attachImage: (type: RecordType, id: number, attachmentIds: number[]) => request<Technique | Product>(`/${type === 'technique' ? 'techniques' : 'products'}/${id}`, json({ attachment_ids: attachmentIds }, 'PATCH')),
  importMarkdown: (file: File, sourceTitle: string, sourceUrl: string) => {
    const body = new FormData()
    body.append('file', file)
    if (sourceTitle.trim()) body.append('source_title', sourceTitle.trim())
    if (sourceUrl.trim()) body.append('source_url', sourceUrl.trim())
    return request<unknown>('/imports/markdown', { method: 'POST', body })
  },
  uploadImage: (file: File) => {
    const body = new FormData()
    body.append('file', file)
    return request<{ id: number; filename: string; content_type: string; url: string }>('/attachments', { method: 'POST', body })
  },
  downloadBackup: async () => {
    const response = await fetch(`${BASE}/backup`)
    if (!response.ok) throw new Error(`备份下载失败（${response.status}）`)
    return response.blob()
  },
  restoreBackup: (file: File) => {
    const body = new FormData()
    body.append('file', file)
    return request<unknown>('/backup', { method: 'POST', body })
  },
}
