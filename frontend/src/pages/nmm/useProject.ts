import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { eventsUrl, nmmApi } from '../../nmmApi'
export function useProject(id: string) {
  const cache = useQueryClient(),
    [disconnected, setDisconnected] = useState(false)
  const query = useQuery({
    queryKey: ['nmm', 'project', id],
    queryFn: () => nmmApi.detail(id),
    refetchInterval: (query) =>
      disconnected &&
      query.state.data &&
      ['generating', 'planning', 'rendering'].includes(query.state.data.status)
        ? 2500
        : false,
  })
  const active =
    !!query.data &&
    ['generating', 'planning', 'rendering'].includes(query.data.status)
  useEffect(() => {
    setDisconnected(false)
    if (!active) return
    const refresh = () => {
      void cache.invalidateQueries({ queryKey: ['nmm', 'project', id] })
      void cache.invalidateQueries({ queryKey: ['nmm', 'list'] })
    }
    if (typeof EventSource === 'undefined') {
      setDisconnected(true)
      return
    }
    const events = new EventSource(eventsUrl(id))
    events.addEventListener('snapshot', refresh)
    events.addEventListener('progress', refresh)
    events.onerror = () => {
      events.close()
      setDisconnected(true)
      refresh()
    }
    return () => events.close()
  }, [id, active, cache])
  return { ...query, disconnected }
}
