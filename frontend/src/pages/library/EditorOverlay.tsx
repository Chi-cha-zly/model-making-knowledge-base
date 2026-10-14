import { X } from 'lucide-react'
import { useState } from 'react'
import { type Product, type Source, type Technique } from '../../api'
import SourceField from './SourceField'
import {
  type Editor,
  emptyProduct,
  type Entry,
  sourceIsPublished,
} from './types'
export default function EditorOverlay({
  editor,
  sources,
  products,
  onClose,
  onSaveProduct,
  onSaveSource,
  busy,
  error,
  onNewSource,
}: {
  editor: NonNullable<Editor>
  sources: Source[]
  selected: Entry | null
  techniques: Technique[]
  products: Product[]
  onClose: () => void
  onSaveProduct: (values: typeof emptyProduct) => void
  onSaveSource: (values: {
    title: string
    url: string
    author: string
    license_note: string
    status: string
  }) => void
  busy: boolean
  error: string
  onNewSource: () => void
}) {
  const product =
    editor.type === 'product'
      ? products.find((item) => item.id === editor.id)
      : undefined
  const source =
    editor.type === 'source'
      ? sources.find((item) => item.id === editor.id)
      : undefined
  const [p, setP] = useState<typeof emptyProduct>(() =>
    product
      ? {
          brand: product.brand,
          name: product.name,
          sku: product.sku || '',
          kind: product.kind || '',
          aliases: product.aliases.join('，'),
          sourceIds: [...product.source_ids],
          status: product.status,
        }
      : emptyProduct,
  )
  const [s, setS] = useState(() => ({
    title: source?.title || '',
    url: source?.url || '',
    author: source?.author || '',
    license_note: source?.license_note || '',
    status: source?.status || 'published',
  }))
  const title =
    editor.type === 'source'
      ? `${editor.id ? '编辑' : '登记'}资料来源`
      : editor.type === 'technique'
        ? `${editor.id ? '编辑' : '新建'}技巧`
        : `${editor.id ? '编辑' : '新建'}产品`
  return (
    <div
      className="overlay"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <section
        className="editor"
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <header>
          <div>
            <span className="page-kicker">资料录入</span>
            <h2>{title}</h2>
          </div>
          <button
            className="icon-button"
            aria-label="关闭编辑器"
            onClick={onClose}
          >
            <X size={21} />
          </button>
        </header>
        {error && (
          <div className="form-error" role="alert">
            {error}
          </div>
        )}
        {editor.type === 'source' ? (
          <form
            onSubmit={(event) => {
              event.preventDefault()
              onSaveSource(s)
            }}
          >
            <label>
              来源标题
              <input
                aria-label="来源标题"
                value={s.title}
                onChange={(event) => setS({ ...s, title: event.target.value })}
                required
              />
            </label>
            <label>
              原文链接
              <input
                type="url"
                value={s.url}
                onChange={(event) => setS({ ...s, url: event.target.value })}
                placeholder="https://"
              />
            </label>
            <label>
              作者或机构
              <input
                value={s.author}
                onChange={(event) => setS({ ...s, author: event.target.value })}
              />
            </label>
            <label>
              授权或引用说明
              <textarea
                value={s.license_note}
                onChange={(event) =>
                  setS({ ...s, license_note: event.target.value })
                }
              />
            </label>
            <label>
              状态
              <select
                value={s.status}
                onChange={(event) => setS({ ...s, status: event.target.value })}
              >
                <option value="published">已发布</option>
                <option value="draft">草稿</option>
                <option value="withdrawn">已撤回</option>
              </select>
            </label>
            <footer>
              <button
                type="button"
                className="outline-button"
                onClick={onClose}
              >
                取消
              </button>
              <button className="primary-button" disabled={busy}>
                保存来源
              </button>
            </footer>
          </form>
        ) : (
          <form
            onSubmit={(event) => {
              event.preventDefault()
              onSaveProduct(p)
            }}
          >
            <div className="form-row">
              <label>
                品牌
                <input
                  value={p.brand}
                  onChange={(event) =>
                    setP({ ...p, brand: event.target.value })
                  }
                  required
                />
              </label>
              <label>
                产品名称
                <input
                  value={p.name}
                  onChange={(event) => setP({ ...p, name: event.target.value })}
                  required
                />
              </label>
            </div>
            <div className="form-row">
              <label>
                产品编号
                <input
                  value={p.sku}
                  onChange={(event) => setP({ ...p, sku: event.target.value })}
                  placeholder="SKU / 型号"
                />
              </label>
              <label>
                产品类型
                <input
                  value={p.kind}
                  onChange={(event) => setP({ ...p, kind: event.target.value })}
                  placeholder="例如：底漆"
                />
              </label>
            </div>
            <label>
              别名
              <input
                value={p.aliases}
                onChange={(event) =>
                  setP({ ...p, aliases: event.target.value })
                }
                placeholder="用逗号分隔"
              />
            </label>
            <label>
              状态
              <select
                value={p.status}
                onChange={(event) => setP({ ...p, status: event.target.value })}
              >
                <option value="published">已发布</option>
                <option value="draft">草稿</option>
                <option value="withdrawn">已撤回</option>
              </select>
            </label>
            <SourceField
              sources={sources}
              value={p.sourceIds}
              onChange={(value) => setP({ ...p, sourceIds: value })}
              onNewSource={onNewSource}
            />
            <footer>
              <button
                type="button"
                className="outline-button"
                onClick={onClose}
              >
                取消
              </button>
              <button
                className="primary-button"
                disabled={busy || !sources.some(sourceIsPublished)}
              >
                保存产品
              </button>
            </footer>
          </form>
        )}
      </section>
    </div>
  )
}
