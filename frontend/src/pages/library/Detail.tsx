import { BookOpenText, ChevronRight, ImagePlus } from 'lucide-react'
import { type Product, type Source, type Technique } from '../../api'
import ArticleBody from '../../ArticleBody'
import { type Entry, sourceName } from './types'
export default function Detail({
  entry,
  sources,
  image,
  onEdit,
  onImage,
  onWithdraw,
  busy,
}: {
  entry: Entry
  sources: Source[]
  image: { name: string; url: string } | null
  onEdit: () => void
  onWithdraw: () => void
  busy: boolean
  onImage: (file: File) => void
}) {
  const technique =
    entry.type === 'technique' ? (entry.item as Technique) : null
  const product = entry.type === 'product' ? (entry.item as Product) : null
  return (
    <div className="detail-content">
      <div className="detail-top">
        <span className="detail-type">
          {technique ? '制作技巧' : '产品档案'}
        </span>
        <button className="detail-edit" disabled={busy} onClick={onEdit}>
          编辑
        </button>
        {technique?.status === 'published' && (
          <button
            className="source-action danger"
            disabled={busy}
            onClick={onWithdraw}
          >
            撤回已发布技巧
          </button>
        )}
      </div>
      <h2>{technique?.title || `${product?.brand} ${product?.name}`}</h2>
      <div className="detail-meta">
        <span>{technique?.category || product?.kind || '产品'}</span>
        <span>
          {entry.item.status === 'published' ? '已发布' : entry.item.status}
        </span>
      </div>
      {technique ? (
        <>
          <p className="detail-summary">{technique.summary || '暂无摘要'}</p>
          <div className="detail-section">
            <ArticleBody
              blocks={technique.content_blocks.flatMap((block) =>
                block.type === 'image'
                  ? block.caption
                    ? [{ type: 'paragraph' as const, text: block.caption }]
                    : []
                  : [block],
              )}
            />
          </div>
          {technique.tags.length > 0 && (
            <div className="tag-list">
              {technique.tags.map((tag) => (
                <span key={tag}>{tag}</span>
              ))}
            </div>
          )}
        </>
      ) : (
        <>
          <div className="product-spec">
            <span>产品编号</span>
            <strong>{product?.sku || '未录入'}</strong>
          </div>
          <div className="product-spec">
            <span>别名</span>
            <strong>{product?.aliases.join('、') || '未录入'}</strong>
          </div>
        </>
      )}
      <div className="detail-section sources">
        <h3>资料来源</h3>
        {entry.item.source_ids.length ? (
          entry.item.source_ids.map((id) => {
            const source = sources.find((item) => item.id === id)
            return (
              <div className="citation" key={id}>
                <BookOpenText size={18} />
                <div>
                  {source?.url ? (
                    <a
                      className="citation-title"
                      href={source.url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      {sourceName(id, sources)} <ChevronRight size={14} />
                    </a>
                  ) : (
                    <strong>{sourceName(id, sources)}</strong>
                  )}
                  <span>
                    {source?.source_type === 'original' ? '个人原创 · ' : ''}
                    {source?.author || '原始资料'}
                  </span>
                </div>
              </div>
            )
          })
        ) : (
          <p className="muted">尚未关联来源</p>
        )}
      </div>
      <div className="detail-section attachment">
        <h3>图片附件</h3>
        {(entry.item.attachment_ids || [])
          .filter(
            (id) =>
              !technique?.content_blocks?.some(
                (block) => block.type === 'image' && block.attachment_id === id,
              ),
          )
          .map((id) => (
            <a
              href={
                image?.url && image.url.endsWith(`/${id}`)
                  ? image.url
                  : `/api/v1/attachments/${id}`
              }
              target="_blank"
              rel="noreferrer"
              key={id}
            >
              {image?.url.endsWith(`/${id}`) ? image.name : `图片 #${id}`}
            </a>
          ))}
        <label className="upload-inline">
          <ImagePlus size={17} />
          上传图片
          <input
            type="file"
            disabled={busy}
            accept="image/png,image/jpeg,image/webp"
            onChange={(event) => {
              const file = event.target.files?.[0]
              if (file) onImage(file)
            }}
          />
        </label>
      </div>
    </div>
  )
}
