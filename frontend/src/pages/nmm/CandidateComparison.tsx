import { useState } from 'react'
import { assetUrl, type NmmAsset } from '../../nmmApi'

export default function CandidateComparison({
  original,
  candidateId,
}: {
  original: NmmAsset | undefined
  candidateId: string
}) {
  const [mode, setMode] = useState<'side' | 'overlay'>('side')
  const [opacity, setOpacity] = useState(50)
  if (!original)
    return (
      <p className="nmm-warning">
        原图暂不可用，无法进行结构对照。请刷新项目。
      </p>
    )
  const aspectRatio = `${original.width} / ${original.height}`
  return (
    <section
      className="nmm-candidate-comparison"
      aria-label="原图与候选结构对照"
    >
      <p>
        逐一对照零件是否缺失或移位、外轮廓是否变形、接缝和浮雕是否被删改。明暗和金属配色的变化属于涂装效果，结构检查仍需您判断。
      </p>
      <div className="nmm-actions" role="group" aria-label="对照方式">
        <button
          type="button"
          aria-pressed={mode === 'side'}
          onClick={() => setMode('side')}
        >
          并排对照
        </button>
        <button
          type="button"
          aria-pressed={mode === 'overlay'}
          onClick={() => setMode('overlay')}
        >
          透明叠加
        </button>
      </div>
      {mode === 'side' ? (
        <div className="nmm-candidate-pair">
          <figure>
            <figcaption>原图</figcaption>
            <div className="nmm-candidate-frame" style={{ aspectRatio }}>
              <img src={assetUrl(original.id)} alt="对照原图" />
            </div>
          </figure>
          <figure>
            <figcaption>候选效果</figcaption>
            <div className="nmm-candidate-frame" style={{ aspectRatio }}>
              <img src={assetUrl(candidateId)} alt="对照候选效果" />
            </div>
          </figure>
        </div>
      ) : (
        <>
          <label>
            候选不透明度
            <input
              aria-label="候选不透明度"
              type="range"
              min={0}
              max={100}
              value={opacity}
              onChange={(event) => setOpacity(Number(event.target.value))}
            />
            <output>{opacity}%</output>
          </label>
          <div className="nmm-candidate-frame" style={{ aspectRatio }}>
            <img src={assetUrl(original.id)} alt="对照原图" />
            <img
              src={assetUrl(candidateId)}
              alt="对照候选效果"
              style={{ opacity: opacity / 100 }}
            />
          </div>
        </>
      )}
    </section>
  )
}
