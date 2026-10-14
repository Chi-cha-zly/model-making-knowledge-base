import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { Archive, BookOpenText, Box, Check, ChevronRight, Download, FileUp, ImagePlus, Layers3, Menu, MessageSquare, Plus, Search, Settings2, Upload, X } from 'lucide-react'
import CreationMenu from './CreationMenu'
import TechniqueEditor, { type TechniqueValues } from './TechniqueEditor'
import ArticleBody from './ArticleBody'
import SettingsWorkbench from './SettingsWorkbench'
import ChatWorkbench from './ChatWorkbench'
import EvaluationWorkbench from './EvaluationWorkbench'
import { api, type Product, type RecordType, type Source, type Technique, type SearchResult } from './api'

type Entry = { type: RecordType; item: Technique | Product }
type Mode = 'browse' | 'manage' | 'chat' | 'evaluation' | 'settings'
type Editor = { type: RecordType; id?: number } | { type: 'source'; id?: number } | null
type Filter = 'all' | 'technique' | 'product'

const categories = ['全部', '素组', '涂装', '横向主题']
const emptyProduct = { brand: '', name: '', sku: '', kind: '', aliases: '', sourceIds: [] as number[], status: 'published' }

function formatError(error: unknown) { return error instanceof Error ? error.message : '操作失败，请重试' }
function sourceName(id: number, sources: Source[]) { return sources.find(source => source.id === id)?.title ?? `来源 #${id}` }
function sourceIsPublished(source: Source) { return !source.status || source.status === 'published' }

export default function App() {
  const [mode, setMode] = useState<Mode>(() => window.location.hash.startsWith('#settings') ? 'settings' : window.location.hash.startsWith('#evaluation') ? 'evaluation' : window.location.hash.startsWith('#chat') ? 'chat' : 'browse')
  const [sources, setSources] = useState<Source[]>([])
  const [techniques, setTechniques] = useState<Technique[]>([])
  const [products, setProducts] = useState<Product[]>([])
  const [results, setResults] = useState<SearchResult | null>(null)
  const [query, setQuery] = useState('')
  const [submittedQuery, setSubmittedQuery] = useState('')
  const [filter, setFilter] = useState<Filter>('all')
  const [category, setCategory] = useState('全部')
  const [selected, setSelected] = useState<Entry | null>(null)
  const [editor, setEditor] = useState<Editor>(null)
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [mobileMenu, setMobileMenu] = useState(false)
  const [image, setImage] = useState<{ name: string; url: string } | null>(null)

  useEffect(() => { if (mode === 'settings') { window.history.replaceState(null, '', window.location.pathname + window.location.search + '#settings'); return } if ((mode !== 'chat' && window.location.hash.startsWith('#chat')) || (mode !== 'evaluation' && window.location.hash.startsWith('#evaluation')) || window.location.hash.startsWith('#settings')) window.history.replaceState(null, '', window.location.pathname + window.location.search) }, [mode])

  async function refresh() {
    const [sourceList, techniqueList, productList] = await Promise.all([api.sources(), api.techniques(), api.products()])
    setSources(sourceList)
    setTechniques(techniqueList)
    setProducts(productList)
  }

  useEffect(() => {
    let active = true
    Promise.all([api.sources(), api.techniques(), api.products()])
      .then(([sourceList, techniqueList, productList]) => { if (active) { setSources(sourceList); setTechniques(techniqueList); setProducts(productList) } })
      .catch(err => { if (active) setError(formatError(err)) })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [])

  async function search(event: FormEvent) {
    event.preventDefault()
    setSelected(null)
    setError('')
    const term = query.trim()
    setSubmittedQuery(term)
    if (!term) { setResults(null); return }
    setBusy(true)
    try { setResults(await api.search(term)) } catch (err) { setError(formatError(err)) } finally { setBusy(false) }
  }

  const visibleRecords = useMemo(() => {
    const activeSources = new Set(sources.filter(source => !source.status || source.status === 'published').map(source => source.id))
    const isVisible = (item: Technique | Product) => item.status === 'published' && item.source_ids.some(id => activeSources.has(id))
    const data = results ?? { techniques, products }
    return { techniques: data.techniques.filter(isVisible), products: data.products.filter(isVisible) }
  }, [results, techniques, products, sources])

  const list = useMemo(() => {
    const data = visibleRecords
    const combined: Entry[] = [
      ...data.techniques.map(item => ({ type: 'technique' as const, item })),
      ...data.products.map(item => ({ type: 'product' as const, item })),
    ]
    if (results?.ranking) {
      const ranks = new Map(results.ranking.map((key, index) => [key, index]))
      combined.sort((a, b) => (ranks.get(`${a.type}:${a.item.id}`) ?? Infinity) - (ranks.get(`${b.type}:${b.item.id}`) ?? Infinity))
    }
    return combined.filter(entry => (filter === 'all' || entry.type === filter) && (category === '全部' || (entry.type === 'technique' && (entry.item as Technique).category === category)))
  }, [visibleRecords, filter, category, results])

  function openEditor(next: Editor) { setEditor(next); setSelected(null); setError(''); setNotice(''); if (next?.type !== 'source' || mode === 'chat' || mode === 'evaluation' || mode === 'settings') setMode('browse') }
  function openEntry(entry: Entry) { setSelected(entry); setEditor(null); setError(''); setImage(null) }

  async function saveTechnique(values: TechniqueValues) {
    const original = !editor?.id && values.original
    if (!original && values.sourceIds.some(id => !sources.some(source => source.id === id && sourceIsPublished(source)))) throw new Error('请先移除或替换不可用来源')
    if (!original && !values.sourceIds.length) throw new Error('请选择资料来源')
    const body = { title: values.title.trim(), category: values.category, summary: values.summary.trim(), steps: [], content_blocks: values.content_blocks, attachment_ids: values.attachment_ids, tags: values.tags.split(/[,，\n]/).map(tag => tag.trim()).filter(Boolean), source_ids: original ? [] : values.sourceIds, status: values.status, ...(!editor?.id ? { original } : {}) }
    if (!body.title) throw new Error('请填写标题')
    const saved = await api.saveTechnique(body, editor?.type === 'technique' ? editor.id : undefined)
    await refresh()
    setResults(null)
    setSubmittedQuery('')
    setEditor(null)
    setSelected(saved.status === 'published' ? { type: 'technique', item: saved } : null)
    if (saved.status !== 'published') setMode('manage')
    setNotice('已保存技巧')
  }

  async function saveProduct(values: typeof emptyProduct) {
    if (values.sourceIds.some(id => !sources.some(source => source.id === id && sourceIsPublished(source)))) throw new Error('请先移除或替换不可用来源')
    if (!values.sourceIds.length) throw new Error('请选择资料来源')
    const body = { brand: values.brand.trim(), name: values.name.trim(), sku: values.sku.trim(), kind: values.kind.trim(), aliases: values.aliases.split(/[,，\n]/).map(alias => alias.trim()).filter(Boolean), source_ids: values.sourceIds, status: values.status }
    if (!body.brand || !body.name) throw new Error('请填写品牌和名称')
    const saved = await api.saveProduct(body, editor?.type === 'product' ? editor.id : undefined)
    await refresh()
    setResults(null)
    setSubmittedQuery('')
    setEditor(null)
    setSelected({ type: 'product', item: saved })
    setNotice('已保存产品')
  }

  async function saveSource(values: { title: string; url: string; author: string; license_note: string; status: string }) {
    if (!values.title.trim()) throw new Error('请填写来源标题')
    const body = { title: values.title.trim(), url: values.url.trim() || null, author: values.author.trim() || null, license_note: values.license_note.trim() || null, status: values.status }
    if (editor?.type === 'source' && editor.id) await api.updateSource(editor.id, body)
    else await api.createSource(body)
    await refresh()
    setEditor(null)
    setNotice(editor?.type === 'source' && editor.id ? '已更新来源' : '已添加来源')
  }

  async function runAction(action: () => Promise<unknown>, success: string) {
    setBusy(true); setError(''); setNotice('')
    try { await action(); await refresh(); setNotice(success) } catch (err) { setError(formatError(err)) } finally { setBusy(false) }
  }

  async function changeTechniqueStatus(item: Technique, publish: boolean) {
    if (!publish && !window.confirm(`撤回“${item.title}”后，它将不再用于检索和对话引用。确定继续吗？`)) return
    await runAction(async () => {
      await (publish ? api.publishTechnique(item.id) : api.withdrawTechnique(item.id))
      setSelected(null)
      setResults(null)
      setSubmittedQuery('')
    }, publish ? '技巧已恢复发布' : '技巧已撤回')
  }

  async function download() {
    setBusy(true); setError('')
    try {
      const blob = await api.downloadBackup()
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `model-knowledge-backup-${new Date().toISOString().slice(0, 10)}.zip`
      link.click()
      URL.revokeObjectURL(url)
      setNotice('备份已下载')
    } catch (err) { setError(formatError(err)) } finally { setBusy(false) }
  }

  return <div className="app-shell">
    <aside className={`sidebar ${mobileMenu ? 'mobile-open' : ''}`}>
      <div className="brand"><div className="brand-mark"><Layers3 size={24} strokeWidth={1.7} /></div><div><strong>模型制作</strong><span>知识库 / WORKBENCH</span></div></div>
      <nav aria-label="主导航" className="main-nav">
        <button className={mode === 'evaluation' ? 'active' : ''} onClick={() => { setMode('evaluation'); setEditor(null); setSelected(null); setMobileMenu(false) }}><Search size={19} />检索测评</button>
        <button className={mode === 'chat' ? 'active' : ''} onClick={() => { setMode('chat'); setEditor(null); setSelected(null); setMobileMenu(false) }}><MessageSquare size={19} />对话助手</button>
        <button className={mode === 'browse' ? 'active' : ''} onClick={() => { setMode('browse'); setMobileMenu(false) }}><BookOpenText size={19} />资料检索</button>
        <button className={mode === 'manage' ? 'active' : ''} onClick={() => { setMode('manage'); setEditor(null); setSelected(null); setMobileMenu(false) }}><Archive size={19} />资料管理</button>
        <button className={mode === 'settings' ? 'active' : ''} onClick={() => { setMode('settings'); setEditor(null); setSelected(null); setMobileMenu(false) }}><Settings2 size={19} />设置</button>
      </nav>
      <div className="sidebar-bottom"><div className="sidebar-note"><span className="pin" />本机资料库<br /><small>每条记录都应有可核对的来源</small></div><span className="sidebar-version">V6 / PERSONAL WORKBENCH</span></div>
    </aside>
    <div className="main-shell">
      <header className="topbar"><button className="mobile-toggle icon-button" aria-label="打开菜单" onClick={() => setMobileMenu(!mobileMenu)}><Menu size={22} /></button><div className="breadcrumb">知识库 <ChevronRight size={14} /> {mode === 'settings' ? '设置' : mode === 'evaluation' ? '检索测评' : mode === 'chat' ? '对话助手' : mode === 'browse' ? '资料检索' : '资料管理'}</div><div className="topbar-actions"><CreationMenu onSelect={type => openEditor({ type })} /></div></header>
      <main>
        {error && <div className="alert error" role="alert"><span>{error}</span><button aria-label="关闭错误" onClick={() => setError('')}><X size={16} /></button></div>}
        {notice && <div className="alert success" role="status"><Check size={17} /><span>{notice}</span><button aria-label="关闭通知" onClick={() => setNotice('')}><X size={16} /></button></div>}
        {mode === 'settings' ? <SettingsWorkbench /> : mode === 'evaluation' ? <EvaluationWorkbench onOpenReference={citation => { const item = citation.type === 'technique' ? techniques.find(item => item.id === citation.id) : products.find(item => item.id === citation.id); if (item && item.status === 'published' && item.source_ids.some(id => sources.some(source => source.id === id && sourceIsPublished(source)))) { setMode('browse'); openEntry({ type: citation.type, item }) } else setError('这条资料已不可用。历史报告保留运行时的引用快照。') }} /> : mode === 'chat' ? <ChatWorkbench onOpenReference={citation => { const item = citation.type === 'technique' ? techniques.find(item => item.id === citation.id) : products.find(item => item.id === citation.id); if (item) { setMode('browse'); openEntry({ type: citation.type, item }) } else setError('这条资料已不可用，请查看参考资料中的原始出处。') }} /> : mode === 'manage' ? <section className="manage-page">
          <div className="page-intro"><div><span className="page-kicker">资料维护</span><h1>让每条记录有据可查。</h1><p>管理来源、导入手稿，并保存整个资料库。</p></div><div className="intro-line" /></div>
          <div className="manage-grid">
            <section className="manage-panel"><div className="panel-heading"><FileUp size={22} /><h2>导入 Markdown</h2></div><p>上传本地 .md 文件，保留原始出处后再整理成技巧。</p><ImportForm busy={busy} onImport={async (file, title, url) => runAction(() => api.importMarkdown(file, title, url), 'Markdown 已导入')} /></section>
            <section className="manage-panel"><div className="panel-heading"><Archive size={22} /><h2>备份与恢复</h2></div><p>备份包含资料和附件。恢复会以所选备份替换当前资料，请先下载一份当前备份。</p><button className="outline-button" disabled={busy} onClick={download}><Download size={17} />下载备份</button><label className="file-button"><Upload size={17} />从 ZIP 恢复<input aria-label="从 ZIP 恢复" type="file" accept=".zip,application/zip" disabled={busy} onChange={event => { const file = event.target.files?.[0]; if (file && window.confirm('恢复备份会替换当前资料库。确定继续吗？')) void runAction(() => api.restoreBackup(file), '资料库已恢复'); event.target.value = '' }} /></label></section>
          </div>
          <div className="source-heading"><div><h2>技巧管理</h2><p>编辑已保存技巧，或恢复发布已撤回的内容。</p></div></div>
          {techniques.length ? <div className="source-list">{techniques.map(item => <div className="source-row" key={item.id}><div className="source-icon"><Layers3 size={18} /></div><div><strong>{item.title}</strong><span>{item.status === 'published' ? '已发布' : item.status === 'withdrawn' ? '已撤回' : '草稿'}{item.source_ids.some(id => sources.some(source => source.id === id && source.source_type === 'original')) ? ' · 个人原创' : ''}</span></div><button className="source-action" aria-label={`编辑技巧${item.title}`} onClick={() => openEditor({ type: 'technique', id: item.id })}>编辑</button>{item.status === 'published' ? <button className="source-action danger" disabled={busy} aria-label={`撤回技巧${item.title}`} onClick={() => void changeTechniqueStatus(item, false)}>撤回</button> : <button className="source-action" disabled={busy} aria-label={`恢复发布${item.title}`} onClick={() => void changeTechniqueStatus(item, true)}>{item.status === 'withdrawn' ? '恢复发布' : '发布'}</button>}</div>)}</div> : <div className="empty-small">尚无技巧。可直接新建个人原创技巧。</div>}
          <div className="source-heading"><div><h2>来源档案</h2><p>{sources.length} 条已登记的原始资料</p></div><button className="outline-button" onClick={() => openEditor({ type: 'source' })}><Plus size={17} />登记来源</button></div>
          {sources.length ? <div className="source-list">{sources.map(source => <div className="source-row" key={source.id}><div className="source-icon"><BookOpenText size={18} /></div><div><strong>{source.title}{source.status === 'withdrawn' && <span className="withdrawn-label">已撤回</span>}</strong><span>{source.source_type === 'original' ? '个人原创 · ' : ''}{source.author || '作者未填写'}{source.license_note ? ` · ${source.license_note}` : ''}</span></div>{source.url && <a href={source.url} target="_blank" rel="noreferrer">打开原文 <ChevronRight size={16} /></a>}<button className="source-action" aria-label={`编辑${source.title}`} onClick={() => openEditor({ type: 'source', id: source.id })}>编辑</button>{source.status !== 'withdrawn' && <button className="source-action danger" aria-label={`撤回${source.title}`} onClick={() => { if (window.confirm(`撤回“${source.title}”后，引用它的资料将不再出现在检索结果中。确定继续吗？`)) void runAction(() => api.withdrawSource(source.id), '来源已撤回') }}>撤回</button>}</div>)}</div> : <div className="empty-small">尚无来源。先登记一本说明书、厂商页面或个人笔记。</div>}
        </section> : <>
          <section className="hero"><div className="hero-copy"><span className="page-kicker">个人模型制作资料库</span><h1>把做过的功课，<br />留给下一次制作。</h1><p>查找技巧、产品与原始出处。编号和别名也能检索。</p></div><div className="hero-graphic" aria-hidden="true"><div className="diagram-grid"><div className="diagram-circle" /><div className="diagram-cross one" /><div className="diagram-cross two" /><div className="diagram-label">REFERENCE / 01</div></div></div></section>
          <form className="search-form" onSubmit={search}><Search size={22} /><input type="search" aria-label="搜索资料" value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索工序、品牌、产品编号或别名…" /><button type="submit" disabled={busy}>搜索</button></form>
          <div className="content-heading"><div><span className="section-caption">INDEX</span><h2>{submittedQuery ? `“${submittedQuery}”的检索结果` : '资料索引'}</h2><p>{loading ? '正在读取资料…' : `共 ${list.length} 条记录，可按类型与工序筛选`}</p>{results?.ranking && <p>按相关性显示前 20 条结果</p>}</div></div>
          <div className="workspace"><aside className="filters"><div className="filter-title"><Settings2 size={17} />筛选资料</div><div className="filter-group"><span>资料类型</span><button aria-label="查看全部" className={filter === 'all' ? 'chosen' : ''} onClick={() => setFilter('all')}>全部资料 <em>{visibleRecords.techniques.length + visibleRecords.products.length}</em></button><button aria-label="只看技巧" className={filter === 'technique' ? 'chosen' : ''} onClick={() => setFilter('technique')}>制作技巧 <em>{visibleRecords.techniques.length}</em></button><button aria-label="只看产品" className={filter === 'product' ? 'chosen' : ''} onClick={() => setFilter('product')}>产品档案 <em>{visibleRecords.products.length}</em></button></div><div className="filter-group"><span>工序分类</span>{categories.map(item => <button key={item} className={category === item ? 'chosen' : ''} onClick={() => setCategory(item)}>{item}</button>)}</div></aside>
          <section className="results" aria-label="资料列表">{loading ? <div className="empty-state">正在读取资料…</div> : list.length ? list.map(entry => <button className={`result-card ${selected?.type === entry.type && selected.item.id === entry.item.id ? 'selected' : ''}`} key={`${entry.type}-${entry.item.id}`} onClick={() => openEntry(entry)}><div className="card-icon">{entry.type === 'technique' ? <Layers3 size={21} /> : <Box size={21} />}</div><div className="card-body"><div className="card-meta"><span>{entry.type === 'technique' ? (entry.item as Technique).category : (entry.item as Product).kind || '产品'}</span><span>{entry.type === 'technique' ? '制作技巧' : '产品档案'}</span></div><h3>{entry.type === 'technique' ? (entry.item as Technique).title : `${(entry.item as Product).brand} ${(entry.item as Product).name}`}</h3><p>{entry.type === 'technique' ? (entry.item as Technique).summary || '查看制作步骤与来源' : (entry.item as Product).aliases.join('、') || '查看产品编号与来源'}</p><div className="card-footer"><span>{entry.type === 'product' ? (entry.item as Product).sku || '未录入编号' : `${entry.item.source_ids.length} 条来源`}</span><ChevronRight size={17} /></div></div></button>) : <div className="empty-state"><div className="empty-glyph"><BookOpenText size={26} /></div><h3>{submittedQuery ? '没有找到匹配资料' : '资料库还是空的'}</h3><p>{submittedQuery ? '试试品牌、型号中的一部分，或清除筛选条件。' : '可直接添加个人原创技巧，或登记来源后添加资料。'}</p><button className="outline-button" onClick={() => { setFilter('all'); setCategory('全部'); setQuery(''); setResults(null); setSubmittedQuery('') }}>{submittedQuery ? '清除搜索' : '查看全部'}</button></div>}</section>
          <aside className="detail-panel" role="region" aria-label="资料详情">{selected ? <Detail entry={selected} sources={sources} image={image} busy={busy} onWithdraw={() => { if (selected.type === 'technique') void changeTechniqueStatus(selected.item as Technique, false) }} onEdit={() => openEditor({ type: selected.type, id: selected.item.id })} onImage={async file => { setBusy(true); setError(''); try { const result = await api.uploadImage(file); const ids = [...(selected.item.attachment_ids || []), result.id]; const updated = await api.attachImage(selected.type, selected.item.id, ids); setSelected({ type: selected.type, item: updated }); setImage({ name: result.filename, url: result.url }); await refresh(); setNotice('图片已附加到资料') } catch (err) { setError(formatError(err)) } finally { setBusy(false) } }} /> : <div className="detail-placeholder"><div className="placeholder-lines"><span /><span /><span /></div><h3>选择一条资料</h3><p>在这里查看步骤、产品编号与原始出处。</p></div>}</aside></div>
        </>}
      </main>
    </div>
    {editor?.type === 'technique' && <TechniqueEditor key={editor.id || 'new'} technique={techniques.find(t => t.id === editor.id)} sources={sources} onClose={() => setEditor(null)} onSave={saveTechnique} />}
    {editor && editor.type !== 'technique' && <EditorOverlay editor={editor} sources={sources} selected={selected} techniques={techniques} products={products} onClose={() => setEditor(null)} onSaveProduct={async values => { setBusy(true); setError(''); try { await saveProduct(values) } catch (err) { setError(formatError(err)) } finally { setBusy(false) } }} onSaveSource={async values => { setBusy(true); setError(''); try { await saveSource(values) } catch (err) { setError(formatError(err)) } finally { setBusy(false) } }} busy={busy} error={error} onNewSource={() => setEditor({ type: 'source' })} />}
  </div>
}

function Detail({ entry, sources, image, onEdit, onImage, onWithdraw, busy }: { entry: Entry; sources: Source[]; image: { name: string; url: string } | null; onEdit: () => void; onWithdraw: () => void; busy: boolean; onImage: (file: File) => void }) {
  const technique = entry.type === 'technique' ? entry.item as Technique : null
  const product = entry.type === 'product' ? entry.item as Product : null
  return <div className="detail-content"><div className="detail-top"><span className="detail-type">{technique ? '制作技巧' : '产品档案'}</span><button className="detail-edit" onClick={onEdit}>编辑</button>{technique?.status === 'published' && <button className="source-action danger" disabled={busy} onClick={onWithdraw}>撤回已发布技巧</button>}</div><h2>{technique?.title || `${product?.brand} ${product?.name}`}</h2><div className="detail-meta"><span>{technique?.category || product?.kind || '产品'}</span><span>{entry.item.status === 'published' ? '已发布' : entry.item.status}</span></div>{technique ? <><p className="detail-summary">{technique.summary || '暂无摘要'}</p><div className="detail-section"><ArticleBody blocks={technique.content_blocks?.length ? technique.content_blocks : technique.steps.map(text => ({type: 'paragraph', text}))} /></div>{technique.tags.length > 0 && <div className="tag-list">{technique.tags.map(tag => <span key={tag}>{tag}</span>)}</div>}</> : <><div className="product-spec"><span>产品编号</span><strong>{product?.sku || '未录入'}</strong></div><div className="product-spec"><span>别名</span><strong>{product?.aliases.join('、') || '未录入'}</strong></div></>}
    <div className="detail-section sources"><h3>资料来源</h3>{entry.item.source_ids.length ? entry.item.source_ids.map(id => { const source = sources.find(item => item.id === id); return <div className="citation" key={id}><BookOpenText size={18} /><div>{source?.url ? <a className="citation-title" href={source.url} target="_blank" rel="noreferrer">{sourceName(id, sources)} <ChevronRight size={14} /></a> : <strong>{sourceName(id, sources)}</strong>}<span>{source?.source_type === 'original' ? '个人原创 · ' : ''}{source?.author || '原始资料'}</span></div></div> }) : <p className="muted">尚未关联来源</p>}</div>
    <div className="detail-section attachment"><h3>图片附件</h3>{(entry.item.attachment_ids || []).filter(id => !technique?.content_blocks?.some(block => block.type === 'image' && block.attachment_id === id)).map(id => <a href={image?.url && image.url.endsWith(`/${id}`) ? image.url : `/api/v1/attachments/${id}`} target="_blank" rel="noreferrer" key={id}>{image?.url.endsWith(`/${id}`) ? image.name : `图片 #${id}`}</a>)}<label className="upload-inline"><ImagePlus size={17} />上传图片<input type="file" accept="image/png,image/jpeg,image/webp" onChange={event => { const file = event.target.files?.[0]; if (file) onImage(file) }} /></label></div></div>
}

function EditorOverlay({ editor, sources, products, onClose, onSaveProduct, onSaveSource, busy, error, onNewSource }: { editor: NonNullable<Editor>; sources: Source[]; selected: Entry | null; techniques: Technique[]; products: Product[]; onClose: () => void; onSaveProduct: (values: typeof emptyProduct) => void; onSaveSource: (values: { title: string; url: string; author: string; license_note: string; status: string }) => void; busy: boolean; error: string; onNewSource: () => void }) {
  const product = editor.type === 'product' ? products.find(item => item.id === editor.id) : undefined
  const source = editor.type === 'source' ? sources.find(item => item.id === editor.id) : undefined
  const [p, setP] = useState<typeof emptyProduct>(() => product ? { brand: product.brand, name: product.name, sku: product.sku || '', kind: product.kind || '', aliases: product.aliases.join('，'), sourceIds: [...product.source_ids], status: product.status } : emptyProduct)
  const [s, setS] = useState(() => ({ title: source?.title || '', url: source?.url || '', author: source?.author || '', license_note: source?.license_note || '', status: source?.status || 'published' }))
  const title = editor.type === 'source' ? `${editor.id ? '编辑' : '登记'}资料来源` : editor.type === 'technique' ? `${editor.id ? '编辑' : '新建'}技巧` : `${editor.id ? '编辑' : '新建'}产品`
  return <div className="overlay" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) onClose() }}><section className="editor" role="dialog" aria-modal="true" aria-label={title}><header><div><span className="page-kicker">资料录入</span><h2>{title}</h2></div><button className="icon-button" aria-label="关闭编辑器" onClick={onClose}><X size={21} /></button></header>{error && <div className="form-error" role="alert">{error}</div>}
    {editor.type === 'source' ? <form onSubmit={event => { event.preventDefault(); onSaveSource(s) }}><label>来源标题<input aria-label="来源标题" value={s.title} onChange={event => setS({ ...s, title: event.target.value })} required /></label><label>原文链接<input type="url" value={s.url} onChange={event => setS({ ...s, url: event.target.value })} placeholder="https://" /></label><label>作者或机构<input value={s.author} onChange={event => setS({ ...s, author: event.target.value })} /></label><label>授权或引用说明<textarea value={s.license_note} onChange={event => setS({ ...s, license_note: event.target.value })} /></label><label>状态<select value={s.status} onChange={event => setS({ ...s, status: event.target.value })}><option value="published">已发布</option><option value="draft">草稿</option><option value="withdrawn">已撤回</option></select></label><footer><button type="button" className="outline-button" onClick={onClose}>取消</button><button className="primary-button" disabled={busy}>保存来源</button></footer></form> : <form onSubmit={event => { event.preventDefault(); onSaveProduct(p) }}><div className="form-row"><label>品牌<input value={p.brand} onChange={event => setP({ ...p, brand: event.target.value })} required /></label><label>产品名称<input value={p.name} onChange={event => setP({ ...p, name: event.target.value })} required /></label></div><div className="form-row"><label>产品编号<input value={p.sku} onChange={event => setP({ ...p, sku: event.target.value })} placeholder="SKU / 型号" /></label><label>产品类型<input value={p.kind} onChange={event => setP({ ...p, kind: event.target.value })} placeholder="例如：底漆" /></label></div><label>别名<input value={p.aliases} onChange={event => setP({ ...p, aliases: event.target.value })} placeholder="用逗号分隔" /></label><label>状态<select value={p.status} onChange={event => setP({ ...p, status: event.target.value })}><option value="published">已发布</option><option value="draft">草稿</option><option value="withdrawn">已撤回</option></select></label><SourceField sources={sources} value={p.sourceIds} onChange={value => setP({ ...p, sourceIds: value })} onNewSource={onNewSource} /><footer><button type="button" className="outline-button" onClick={onClose}>取消</button><button className="primary-button" disabled={busy || !sources.some(sourceIsPublished)}>保存产品</button></footer></form>}</section></div>
}

function SourceField({ sources, value, onChange, onNewSource }: { sources: Source[]; value: number[]; onChange: (value: number[]) => void; onNewSource: () => void }) {
  const published = sources.filter(sourceIsPublished)
  const inactiveIds = value.filter(id => !published.some(source => source.id === id))
  return <fieldset className="source-field"><legend>资料来源</legend><div className="source-options">{published.map(source => <label key={source.id} className="source-option"><input type="checkbox" aria-label={source.title} checked={value.includes(source.id)} onChange={event => onChange(event.target.checked ? [...value, source.id] : value.filter(id => id !== source.id))} /><span>{source.title}{source.source_type === 'original' ? ' · 个人原创' : ''}</span></label>)}</div>{!published.length && <p>没有已发布来源。请先登记或发布一条来源。</p>}{inactiveIds.length > 0 && <div className="inactive-sources"><p>以下已关联来源不可用于保存，请明确移除或替换：</p>{inactiveIds.map(id => { const source = sources.find(item => item.id === id); return <div key={id}><span>{source?.title || `来源 #${id}`}（{source?.status === 'withdrawn' ? '已撤回' : source?.status === 'draft' ? '草稿' : '已失效'}）</span><button type="button" aria-label={`移除${source?.title || `来源 #${id}`}`} onClick={() => onChange(value.filter(item => item !== id))}>移除</button></div> })}</div>}<button type="button" onClick={onNewSource}>+ 登记新来源</button></fieldset>
}

function ImportForm({ busy, onImport }: { busy: boolean; onImport: (file: File, title: string, url: string) => void }) {
  const [file, setFile] = useState<File | null>(null)
  const [title, setTitle] = useState('')
  const [url, setUrl] = useState('')
  return <form className="import-form" onSubmit={event => { event.preventDefault(); if (file) onImport(file, title, url) }}><label className="dropzone"><FileUp size={25} /><strong>{file?.name || '选择 Markdown 文件'}</strong><span>支持 .md，导入后可继续修订</span><input type="file" accept=".md,text/markdown,text/plain" onChange={event => setFile(event.target.files?.[0] || null)} /></label><label>来源标题<input value={title} onChange={event => setTitle(event.target.value)} placeholder="例如：个人制作笔记" /></label><label>来源链接（可选）<input type="url" value={url} onChange={event => setUrl(event.target.value)} placeholder="https://" /></label><button className="primary-button" disabled={!file || busy}>开始导入</button></form>
}
