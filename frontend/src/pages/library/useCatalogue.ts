import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type Product, type RecordType, type Technique } from '../../api'

export function useCatalogue() {
  const client = useQueryClient()
  const sources = useQuery({
    queryKey: ['catalogue', 'sources'],
    queryFn: api.sources,
  })
  const techniques = useQuery({
    queryKey: ['catalogue', 'techniques'],
    queryFn: api.techniques,
  })
  const products = useQuery({
    queryKey: ['catalogue', 'products'],
    queryFn: api.products,
  })
  async function updateRecord(type: RecordType, record: Technique | Product) {
    const queryKey = [
      'catalogue',
      type === 'technique' ? 'techniques' : 'products',
    ]
    await client.cancelQueries({ queryKey })
    client.setQueryData<Array<Technique | Product>>(queryKey, (current) =>
      current?.map((item) => (item.id === record.id ? record : item)),
    )
  }
  async function refresh() {
    await client.invalidateQueries(
      { queryKey: ['catalogue'] },
      { throwOnError: true },
    )
  }
  return {
    sources: sources.data ?? [],
    techniques: techniques.data ?? [],
    products: products.data ?? [],
    loading: sources.isPending || techniques.isPending || products.isPending,
    loadError: sources.error || techniques.error || products.error,
    refresh,
    updateRecord,
  }
}
