import { useEffect, useRef, useState } from 'react'
import { nmmApi } from '../../nmmApi'
export default function ExportButton({ projectId }: { projectId: string }) {
  const [busy, setBusy] = useState(false),
    [error, setError] = useState('')
  const mounted = useRef(true),
    gate = useRef(false),
    urls = useRef(new Map<string, number>())
  useEffect(() => {
    mounted.current = true
    const activeUrls = urls.current
    return () => {
      mounted.current = false
      for (const [url, timer] of activeUrls) {
        window.clearTimeout(timer)
        URL.revokeObjectURL(url)
      }
      activeUrls.clear()
    }
  }, [])
  async function download() {
    if (gate.current) return
    gate.current = true
    setBusy(true)
    setError('')
    try {
      const blob = await nmmApi.downloadExport(projectId)
      if (!mounted.current) return
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = 'nmm-output.zip'
      document.body.append(anchor)
      try {
        anchor.click()
      } finally {
        anchor.remove()
        urls.current.set(
          url,
          window.setTimeout(() => {
            URL.revokeObjectURL(url)
            urls.current.delete(url)
          }, 1000),
        )
      }
    } catch (err) {
      if (mounted.current)
        setError(
          err instanceof Error ? err.message : '成果包下载失败，请重试。',
        )
    } finally {
      gate.current = false
      if (mounted.current) setBusy(false)
    }
  }
  return (
    <>
      <button
        className="primary-button"
        disabled={busy}
        onClick={() => void download()}
      >
        {busy ? '正在准备下载…' : '下载成果 ZIP'}
      </button>
      {error && (
        <p className="nmm-error" role="alert">
          {error}
        </p>
      )}
    </>
  )
}
