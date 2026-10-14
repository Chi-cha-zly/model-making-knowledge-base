import { type Source } from '../../api'
import { sourceIsPublished } from './types'
export default function SourceField({
  sources,
  value,
  onChange,
  onNewSource,
}: {
  sources: Source[]
  value: number[]
  onChange: (value: number[]) => void
  onNewSource: () => void
}) {
  const published = sources.filter(sourceIsPublished)
  const inactiveIds = value.filter(
    (id) => !published.some((source) => source.id === id),
  )
  return (
    <fieldset className="source-field">
      <legend>资料来源</legend>
      <div className="source-options">
        {published.map((source) => (
          <label key={source.id} className="source-option">
            <input
              type="checkbox"
              aria-label={source.title}
              checked={value.includes(source.id)}
              onChange={(event) =>
                onChange(
                  event.target.checked
                    ? [...value, source.id]
                    : value.filter((id) => id !== source.id),
                )
              }
            />
            <span>
              {source.title}
              {source.source_type === 'original' ? ' · 个人原创' : ''}
            </span>
          </label>
        ))}
      </div>
      {!published.length && <p>没有已发布来源。请先登记或发布一条来源。</p>}
      {inactiveIds.length > 0 && (
        <div className="inactive-sources">
          <p>以下已关联来源不可用于保存，请明确移除或替换：</p>
          {inactiveIds.map((id) => {
            const source = sources.find((item) => item.id === id)
            return (
              <div key={id}>
                <span>
                  {source?.title || `来源 #${id}`}（
                  {source?.status === 'withdrawn'
                    ? '已撤回'
                    : source?.status === 'draft'
                      ? '草稿'
                      : '已失效'}
                  ）
                </span>
                <button
                  type="button"
                  aria-label={`移除${source?.title || `来源 #${id}`}`}
                  onClick={() => onChange(value.filter((item) => item !== id))}
                >
                  移除
                </button>
              </div>
            )
          })}
        </div>
      )}
      <button type="button" onClick={onNewSource}>
        + 登记新来源
      </button>
    </fieldset>
  )
}
