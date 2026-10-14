import {
  BookOpenText,
  Box,
  ChevronRight,
  Layers3,
  Search,
  Settings2,
} from 'lucide-react'
import { api, type Product, type Technique } from '../../api'
import Detail from './Detail'
import { formatError } from './types'
import type { LibraryController } from './useLibrary'
const categories = ['全部', '素组', '涂装', '横向主题']
export default function BrowsePage({
  sources,
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
  search,
  setResults,
  setSubmittedQuery,
  setBusy,
  setError,
  setSelected,
  setImage,
  refresh,
  setNotice,
}: LibraryController) {
  return (
    <>
      <section className="hero">
        <div className="hero-copy">
          <span className="page-kicker">个人模型制作资料库</span>
          <h1>
            把做过的功课，
            <br />
            留给下一次制作。
          </h1>
          <p>查找技巧、产品与原始出处。编号和别名也能检索。</p>
        </div>
        <div className="hero-graphic" aria-hidden="true">
          <div className="diagram-grid">
            <div className="diagram-circle" />
            <div className="diagram-cross one" />
            <div className="diagram-cross two" />
            <div className="diagram-label">REFERENCE / 01</div>
          </div>
        </div>
      </section>
      <form className="search-form" onSubmit={search}>
        <Search size={22} />
        <input
          type="search"
          aria-label="搜索资料"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="搜索工序、品牌、产品编号或别名…"
        />
        <button type="submit" disabled={busy}>
          搜索
        </button>
      </form>
      <div className="content-heading">
        <div>
          <span className="section-caption">INDEX</span>
          <h2>
            {submittedQuery ? `“${submittedQuery}”的检索结果` : '资料索引'}
          </h2>
          <p>
            {loading
              ? '正在读取资料…'
              : `共 ${list.length} 条记录，可按类型与工序筛选`}
          </p>
          {results?.ranking && <p>按相关性显示前 20 条结果</p>}
        </div>
      </div>
      <div className="workspace">
        <aside className="filters">
          <div className="filter-title">
            <Settings2 size={17} />
            筛选资料
          </div>
          <div className="filter-group">
            <span>资料类型</span>
            <button
              aria-label="查看全部"
              className={filter === 'all' ? 'chosen' : ''}
              onClick={() => setFilter('all')}
            >
              全部资料{' '}
              <em>
                {visibleRecords.techniques.length +
                  visibleRecords.products.length}
              </em>
            </button>
            <button
              aria-label="只看技巧"
              className={filter === 'technique' ? 'chosen' : ''}
              onClick={() => setFilter('technique')}
            >
              制作技巧 <em>{visibleRecords.techniques.length}</em>
            </button>
            <button
              aria-label="只看产品"
              className={filter === 'product' ? 'chosen' : ''}
              onClick={() => setFilter('product')}
            >
              产品档案 <em>{visibleRecords.products.length}</em>
            </button>
          </div>
          <div className="filter-group">
            <span>工序分类</span>
            {categories.map((item) => (
              <button
                key={item}
                className={category === item ? 'chosen' : ''}
                onClick={() => setCategory(item)}
              >
                {item}
              </button>
            ))}
          </div>
        </aside>
        <section className="results" aria-label="资料列表">
          {loading ? (
            <div className="empty-state">正在读取资料…</div>
          ) : list.length ? (
            list.map((entry) => (
              <button
                className={`result-card ${selected?.type === entry.type && selected.item.id === entry.item.id ? 'selected' : ''}`}
                key={`${entry.type}-${entry.item.id}`}
                onClick={() => openEntry(entry)}
              >
                <div className="card-icon">
                  {entry.type === 'technique' ? (
                    <Layers3 size={21} />
                  ) : (
                    <Box size={21} />
                  )}
                </div>
                <div className="card-body">
                  <div className="card-meta">
                    <span>
                      {entry.type === 'technique'
                        ? (entry.item as Technique).category
                        : (entry.item as Product).kind || '产品'}
                    </span>
                    <span>
                      {entry.type === 'technique' ? '制作技巧' : '产品档案'}
                    </span>
                  </div>
                  <h3>
                    {entry.type === 'technique'
                      ? (entry.item as Technique).title
                      : `${(entry.item as Product).brand} ${(entry.item as Product).name}`}
                  </h3>
                  <p>
                    {entry.type === 'technique'
                      ? (entry.item as Technique).summary ||
                        '查看制作步骤与来源'
                      : (entry.item as Product).aliases.join('、') ||
                        '查看产品编号与来源'}
                  </p>
                  <div className="card-footer">
                    <span>
                      {entry.type === 'product'
                        ? (entry.item as Product).sku || '未录入编号'
                        : `${entry.item.source_ids.length} 条来源`}
                    </span>
                    <ChevronRight size={17} />
                  </div>
                </div>
              </button>
            ))
          ) : (
            <div className="empty-state">
              <div className="empty-glyph">
                <BookOpenText size={26} />
              </div>
              <h3>{submittedQuery ? '没有找到匹配资料' : '资料库还是空的'}</h3>
              <p>
                {submittedQuery
                  ? '试试品牌、型号中的一部分，或清除筛选条件。'
                  : '可直接添加个人原创技巧，或登记来源后添加资料。'}
              </p>
              <button
                className="outline-button"
                onClick={() => {
                  setFilter('all')
                  setCategory('全部')
                  setQuery('')
                  setResults(null)
                  setSubmittedQuery('')
                }}
              >
                {submittedQuery ? '清除搜索' : '查看全部'}
              </button>
            </div>
          )}
        </section>
        <aside className="detail-panel" role="region" aria-label="资料详情">
          {selected ? (
            <Detail
              entry={selected}
              sources={sources}
              image={image}
              busy={busy}
              onWithdraw={() => {
                if (selected.type === 'technique')
                  void changeTechniqueStatus(selected.item as Technique, false)
              }}
              onEdit={() =>
                openEditor({ type: selected.type, id: selected.item.id })
              }
              onImage={async (file) => {
                setBusy(true)
                setError('')
                try {
                  const result = await api.uploadImage(file)
                  const ids = [
                    ...(selected.item.attachment_ids || []),
                    result.id,
                  ]
                  const updated = await api.attachImage(
                    selected.type,
                    selected.item.id,
                    ids,
                  )
                  setSelected({ type: selected.type, item: updated })
                  setImage({ name: result.filename, url: result.url })
                  await refresh()
                  setNotice('图片已附加到资料')
                } catch (err) {
                  setError(formatError(err))
                } finally {
                  setBusy(false)
                }
              }}
            />
          ) : (
            <div className="detail-placeholder">
              <div className="placeholder-lines">
                <span />
                <span />
                <span />
              </div>
              <h3>选择一条资料</h3>
              <p>在这里查看步骤、产品编号与原始出处。</p>
            </div>
          )}
        </aside>
      </div>
    </>
  )
}
