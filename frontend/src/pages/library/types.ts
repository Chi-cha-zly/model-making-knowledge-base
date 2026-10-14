import type { Product, RecordType, Source, Technique } from '../../api'
export type Entry = { type: RecordType; item: Technique | Product }
export type Editor =
  { type: RecordType; id?: number } | { type: 'source'; id?: number } | null
export type Filter = 'all' | 'technique' | 'product'

export const emptyProduct = {
  brand: '',
  name: '',
  sku: '',
  kind: '',
  aliases: '',
  sourceIds: [] as number[],
  status: 'published',
}

export function formatError(error: unknown) {
  return error instanceof Error ? error.message : '操作失败，请重试'
}
export function sourceName(id: number, sources: Source[]) {
  return sources.find((source) => source.id === id)?.title ?? `来源 #${id}`
}
export function sourceIsPublished(source: Source) {
  return !source.status || source.status === 'published'
}
