import { useState } from 'react'
import { assetUrl, type NmmProject } from '../../nmmApi'
import NmmDialog from './NmmDialog'
import ExportButton from './ExportButton'
function AssetImage({
  id,
  alt,
  className,
}: {
  id: string
  alt: string
  className?: string
}) {
  const [failed, setFailed] = useState(false)
  return failed ? (
    <p role="status">{alt}暂时无法显示，请刷新重试。</p>
  ) : (
    <img
      className={className}
      src={assetUrl(id)}
      alt={alt}
      onError={() => setFailed(true)}
    />
  )
}
const fields = [
  ['color_description', '颜色说明'],
  ['areas', '涂装区域'],
  ['technique', '操作技法'],
  ['expected_result', '预期效果'],
  ['change_from_previous', '与上一步的变化'],
  ['common_mistakes', '常见错误'],
] as const
export default function ProjectResults({ project }: { project: NmmProject }) {
  const [index, setIndex] = useState(0),
    [diff, setDiff] = useState(false),
    [ratio, setRatio] = useState(100),
    [zoom, setZoom] = useState<string | null>(null),
    [failedDiff, setFailedDiff] = useState<string | null>(null)
  const steps = project.steps,
    step = steps[index] ?? steps[0]
  const original = project.assets.find((asset) => asset.role === 'original'),
    final = project.assets.find((asset) => asset.role === 'final')
  const current =
    step?.image_asset_id ??
    final?.id ??
    project.candidate_ids[0] ??
    original?.id
  const contact = project.assets.find((asset) => asset.role === 'contact_sheet')
  const colors = step?.description.color_hex
    ? [step.description.color_hex].flat()
    : []
  const currentLabel = step
    ? `步骤 ${step.index}：${step.title}`
    : final
      ? '最终效果'
      : project.candidate_ids.length
        ? '候选效果'
        : '原始模型'
  return (
    <section className="nmm-results">
      {current ? (
        <>
          <div
            className="nmm-image-stage"
            style={{
              aspectRatio:
                original?.width && original?.height
                  ? `${original.width} / ${original.height}`
                  : '4 / 3',
            }}
          >
            {original && (
              <AssetImage
                key={`base-${original.id}`}
                id={original.id}
                alt="原始模型照片"
              />
            )}
            <div
              className="nmm-image-layer"
              style={{
                clipPath: original ? `inset(0 ${100 - ratio}% 0 0)` : undefined,
              }}
            >
              <AssetImage key={current} id={current} alt={currentLabel} />
            </div>
            {diff &&
              step?.diff_asset_id &&
              failedDiff !== step.diff_asset_id && (
                <>
                  <img
                    className="nmm-diff-probe"
                    src={assetUrl(step.diff_asset_id)}
                    alt=""
                    onError={() => setFailedDiff(step.diff_asset_id)}
                  />
                  <div
                    role="img"
                    aria-label="本步变化区域"
                    className="nmm-diff-overlay"
                    style={{
                      maskImage: `url("${assetUrl(step.diff_asset_id)}")`,
                      maskMode: 'luminance',
                    }}
                  />
                </>
              )}
          </div>
          {failedDiff === step?.diff_asset_id && (
            <p role="status">变化遮罩暂时无法读取。</p>
          )}
          <div className="nmm-actions">
            <button onClick={() => setZoom(current)}>放大图片</button>
            <a href={`${assetUrl(current)}?download=1`}>
              {step ? '下载本步图片' : '下载当前图片'}
            </a>
            {final && (
              <a href={`${assetUrl(final.id)}?download=1`}>下载最终效果</a>
            )}
          </div>
          {original && current !== original.id && (
            <label className="nmm-compare">
              原图 / 当前效果比较
              <input
                type="range"
                min={0}
                max={100}
                value={ratio}
                onChange={(e) => setRatio(Number(e.target.value))}
                aria-valuetext={`当前效果显示 ${ratio}%`}
              />
            </label>
          )}
        </>
      ) : (
        <div className="nmm-empty-stage">项目图片尚未就绪。</div>
      )}
      {steps.length > 0 && (
        <>
          <nav className="nmm-step-strip" aria-label="涂装步骤">
            {steps.map((item, i) => (
              <button
                key={item.id}
                aria-label={`查看步骤 ${item.index}`}
                aria-pressed={i === index}
                onClick={() => {
                  setIndex(i)
                  setDiff(false)
                  setRatio(100)
                }}
              >
                <img
                  loading="lazy"
                  src={assetUrl(item.image_asset_id)}
                  alt=""
                />
                <span>
                  {item.index}. {item.title}
                </span>
              </button>
            ))}
          </nav>
          {step && (
            <section className="nmm-instruction" aria-label="本步教学说明">
              <div className="nmm-actions">
                <h2>
                  {step.index}. {step.title}
                </h2>
                {step.diff_asset_id && (
                  <label className="nmm-check">
                    <input
                      type="checkbox"
                      checked={diff}
                      onChange={(e) => setDiff(e.target.checked)}
                    />
                    显示本步变化
                  </label>
                )}
              </div>
              {colors.length > 0 && (
                <div className="nmm-swatches">
                  {colors.map((color, i) => (
                    <span key={i}>
                      <i
                        style={{
                          background: /^#[0-9a-f]{6}$/i.test(color)
                            ? color
                            : undefined,
                        }}
                      />
                      {color}
                    </span>
                  ))}
                </div>
              )}
              <p className="nmm-hint">
                变化区域比例：
                {Math.round(step.description.area_ratio * 1000) / 10}%
              </p>
              {!step.description.technique && (
                <p className="nmm-warning">
                  当前仅有算法分层信息，尚无完整教学说明；请结合模型实际表面判断。
                </p>
              )}
              <dl>
                {fields.map(
                  ([key, label]) =>
                    step.description[key] && (
                      <div key={key}>
                        <dt>{label}</dt>
                        <dd>{step.description[key]}</dd>
                      </div>
                    ),
                )}
              </dl>
              {step.description.needs_confirmation?.map((text, i) => (
                <p className="nmm-warning" key={i}>
                  待确认：{text}
                </p>
              ))}
              {step.warnings.map((text, i) => (
                <p className="nmm-warning" key={i}>
                  {text}
                </p>
              ))}
            </section>
          )}
        </>
      )}
      {project.plan?.plan.overall_tips?.length ? (
        <section>
          <h2>整体提示</h2>
          <ul>
            {project.plan.plan.overall_tips.map((tip, i) => (
              <li key={i}>{tip}</li>
            ))}
          </ul>
        </section>
      ) : null}
      {contact && (
        <section className="nmm-contact">
          <h2>步骤汇总</h2>
          <button onClick={() => setZoom(contact.id)} aria-label="放大步骤汇总">
            <AssetImage id={contact.id} alt="步骤汇总图" />
          </button>
          <a href={`${assetUrl(contact.id)}?download=1`}>下载汇总图</a>
        </section>
      )}
      {(steps.length > 0 || final) &&
        !['generating', 'planning', 'rendering'].includes(project.status) && (
          <div className="nmm-export">
            <ExportButton projectId={project.id} />
            <p>
              包含当前版本的效果图、步骤图和教学
              JSON；这是输出成果包，不是项目备份，不包含原始参考资料。
            </p>
          </div>
        )}
      {zoom && (
        <NmmDialog title="查看大图" onClose={() => setZoom(null)}>
          <AssetImage key={zoom} id={zoom} alt="涂装图片放大预览" />
        </NmmDialog>
      )}
    </section>
  )
}
