import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { api, type Product, type SearchResult, type Technique } from '../../api'
import type { TechniqueValues } from '../../TechniqueEditor'
import {
  emptyProduct,
  formatError,
  sourceIsPublished,
  type Editor,
  type Entry,
  type Filter,
} from './types'
import { useCatalogue } from './useCatalogue'
export function useLibrary(mode: string, setMode: (mode: string) => void) {
  const {
    sources,
    techniques,
    products,
    loading,
    loadError,
    refresh,
    updateRecord,
  } = useCatalogue()
  const [results, setResults] = useState<SearchResult | null>(null)
  const [query, setQueryState] = useState('')
  const searchRequest = useRef(0)
  const [searchBusy, setSearchBusy] = useState(false)
  function setQuery(value: string) {
    searchRequest.current++
    setQueryState(value)
    setResults(null)
    setSubmittedQuery('')
    setSearchBusy(false)
  }
  useEffect(
    () => () => {
      searchRequest.current++
    },
    [],
  )
  const [submittedQuery, setSubmittedQuery] = useState('')
  const [filter, setFilter] = useState<Filter>('all')
  const [category, setCategory] = useState('全部')
  const [selected, setSelected] = useState<Entry | null>(null)
  const [editor, setEditor] = useState<Editor>(null)
  const [operationBusy, setBusy] = useState(false)
  const [uploading, setUploading] = useState(false)
  const busy = operationBusy || uploading || searchBusy
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [image, setImage] = useState<{ name: string; url: string } | null>(null)

  useEffect(() => {
    if (loadError) setError(formatError(loadError))
  }, [loadError])

  async function search(event: FormEvent) {
    event.preventDefault()
    setSelected(null)
    setError('')
    const token = ++searchRequest.current
    const term = query.trim()
    setResults(null)
    setSubmittedQuery(term)
    if (!term) {
      setResults(null)
      return
    }
    setSearchBusy(true)
    try {
      const next = await api.search(term)
      if (token === searchRequest.current) setResults(next)
    } catch (err) {
      if (token === searchRequest.current) setError(formatError(err))
    } finally {
      if (token === searchRequest.current) setSearchBusy(false)
    }
  }

  const visibleRecords = useMemo(() => {
    const activeSources = new Set(
      sources
        .filter((source) => !source.status || source.status === 'published')
        .map((source) => source.id),
    )
    const isVisible = (item: Technique | Product) =>
      item.status === 'published' &&
      item.source_ids.some((id) => activeSources.has(id))
    const data = results ?? { techniques, products }
    return {
      techniques: data.techniques.filter(isVisible),
      products: data.products.filter(isVisible),
    }
  }, [results, techniques, products, sources])

  const list = useMemo(() => {
    const data = visibleRecords
    const combined: Entry[] = [
      ...data.techniques.map((item) => ({ type: 'technique' as const, item })),
      ...data.products.map((item) => ({ type: 'product' as const, item })),
    ]
    if (results?.ranking) {
      const ranks = new Map(results.ranking.map((key, index) => [key, index]))
      combined.sort(
        (a, b) =>
          (ranks.get(`${a.type}:${a.item.id}`) ?? Infinity) -
          (ranks.get(`${b.type}:${b.item.id}`) ?? Infinity),
      )
    }
    return combined.filter(
      (entry) =>
        (filter === 'all' || entry.type === filter) &&
        (category === '全部' ||
          (entry.type === 'technique' &&
            (entry.item as Technique).category === category)),
    )
  }, [visibleRecords, filter, category, results])

  function openEditor(next: Editor) {
    if (busy) return
    setEditor(next)
    setSelected(null)
    setError('')
    setNotice('')
    if (
      next?.type !== 'source' ||
      mode === 'chat' ||
      mode === 'evaluation' ||
      mode === 'settings'
    )
      setMode('browse')
  }
  function openEntry(entry: Entry) {
    setSelected(entry)
    setEditor(null)
    setError('')
    setImage(null)
  }

  async function saveTechnique(values: TechniqueValues) {
    const original = !editor?.id && values.original
    if (
      !original &&
      values.sourceIds.some(
        (id) =>
          !sources.some(
            (source) => source.id === id && sourceIsPublished(source),
          ),
      )
    )
      throw new Error('请先移除或替换不可用来源')
    if (!original && !values.sourceIds.length) throw new Error('请选择资料来源')
    const body = {
      title: values.title.trim(),
      category: values.category,
      summary: values.summary.trim(),
      content_blocks: values.content_blocks,
      attachment_ids: values.attachment_ids,
      tags: values.tags
        .split(/[,，\n]/)
        .map((tag) => tag.trim())
        .filter(Boolean),
      source_ids: original ? [] : values.sourceIds,
      status: values.status,
      ...(!editor?.id ? { original } : {}),
    }
    if (!body.title) throw new Error('请填写标题')
    const saved = await api.saveTechnique(
      body,
      editor?.type === 'technique' ? editor.id : undefined,
    )
    await refresh()
    setResults(null)
    setSubmittedQuery('')
    setEditor(null)
    setSelected(
      saved.status === 'published' ? { type: 'technique', item: saved } : null,
    )
    if (saved.status !== 'published') setMode('manage')
    setNotice('已保存技巧')
  }

  async function saveProduct(values: typeof emptyProduct) {
    if (
      values.sourceIds.some(
        (id) =>
          !sources.some(
            (source) => source.id === id && sourceIsPublished(source),
          ),
      )
    )
      throw new Error('请先移除或替换不可用来源')
    if (!values.sourceIds.length) throw new Error('请选择资料来源')
    const body = {
      brand: values.brand.trim(),
      name: values.name.trim(),
      sku: values.sku.trim(),
      kind: values.kind.trim(),
      aliases: values.aliases
        .split(/[,，\n]/)
        .map((alias) => alias.trim())
        .filter(Boolean),
      source_ids: values.sourceIds,
      status: values.status,
    }
    if (!body.brand || !body.name) throw new Error('请填写品牌和名称')
    const saved = await api.saveProduct(
      body,
      editor?.type === 'product' ? editor.id : undefined,
    )
    await refresh()
    setResults(null)
    setSubmittedQuery('')
    setEditor(null)
    setSelected({ type: 'product', item: saved })
    setNotice('已保存产品')
  }

  async function saveSource(values: {
    title: string
    url: string
    author: string
    license_note: string
    status: string
  }) {
    if (!values.title.trim()) throw new Error('请填写来源标题')
    const body = {
      title: values.title.trim(),
      url: values.url.trim() || null,
      author: values.author.trim() || null,
      license_note: values.license_note.trim() || null,
      status: values.status,
    }
    if (editor?.type === 'source' && editor.id)
      await api.updateSource(editor.id, body)
    else await api.createSource(body)
    await refresh()
    setEditor(null)
    setNotice(
      editor?.type === 'source' && editor.id ? '已更新来源' : '已添加来源',
    )
  }

  async function runAction(action: () => Promise<unknown>, success: string) {
    setBusy(true)
    setError('')
    setNotice('')
    try {
      await action()
      await refresh()
      setNotice(success)
    } catch (err) {
      setError(formatError(err))
    } finally {
      setBusy(false)
    }
  }

  async function changeTechniqueStatus(item: Technique, publish: boolean) {
    if (
      !publish &&
      !window.confirm(
        `撤回“${item.title}”后，它将不再用于检索和对话引用。确定继续吗？`,
      )
    )
      return
    await runAction(
      async () => {
        await (publish
          ? api.publishTechnique(item.id)
          : api.withdrawTechnique(item.id))
        setSelected(null)
        setResults(null)
        setSubmittedQuery('')
      },
      publish ? '技巧已恢复发布' : '技巧已撤回',
    )
  }

  async function download() {
    setBusy(true)
    setError('')
    try {
      const blob = await api.downloadBackup()
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `model-knowledge-backup-${new Date().toISOString().slice(0, 10)}.zip`
      link.click()
      URL.revokeObjectURL(url)
      setNotice('备份已下载')
    } catch (err) {
      setError(formatError(err))
    } finally {
      setBusy(false)
    }
  }

  return {
    sources,
    techniques,
    products,
    results,
    query,
    setQuery,
    submittedQuery,
    filter,
    setFilter,
    category,
    setCategory,
    selected,
    busy,
    loading,
    image,
    list,
    visibleRecords,
    openEditor,
    openEntry,
    changeTechniqueStatus,
    runAction,
    download,
    search,
    setResults,
    setSubmittedQuery,
    setBusy,
    setUploading,
    setError,
    setSelected,
    setImage,
    refresh,
    updateRecord,
    setNotice,
    editor,
    setEditor,
    error,
    notice,
    saveTechnique,
    saveProduct,
    saveSource,
  }
}
export type LibraryController = ReturnType<typeof useLibrary>
