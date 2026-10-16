import { useState } from 'react'
import type { CostAuthorization, NmmEstimate } from '../../nmmApi'
import NmmDialog from './NmmDialog'
export default function CostDialog({
  estimate,
  selection,
  resuming,
  cachedRecovery,
  onApprove,
  onClose,
}: {
  estimate: NmmEstimate
  selection: boolean
  resuming: boolean
  cachedRecovery: boolean
  onApprove: (authorization?: CostAuthorization) => void
  onClose: () => void
}) {
  const live = estimate.provider_mode === 'live',
    visionOnly = selection || resuming || cachedRecovery
  const imageCap = Math.min(
    6,
    estimate.remaining_image_calls ?? estimate.remaining_generations,
  )
  const visionCap = Math.min(30, estimate.remaining_vision_calls ?? 30)
  const [images, setImages] = useState(visionOnly ? 0 : Math.min(1, imageCap)),
    [vision, setVision] = useState(
      Math.min(
        cachedRecovery ? 1 : visionOnly ? 4 : estimate.expected_vision_calls,
        visionCap,
      ),
    ),
    [accepted, setAccepted] = useState(false)
  const valid =
    Number.isInteger(images) &&
    Number.isInteger(vision) &&
    images >= 0 &&
    images <= imageCap &&
    vision >= 0 &&
    vision <= visionCap &&
    images + vision > 0 &&
    (visionOnly || images >= 1) &&
    (!visionOnly || vision >= 1)
  return (
    <NmmDialog title="确认本次调用" onClose={onClose}>
      <p>
        {cachedRecovery
          ? '恢复已保存结果，当前预计无需新的模型调用。视觉额度用于后续允许的分析，次数上限不是费用。'
          : selection
            ? '选择候选不会再次生成图片；后续教学分析仍可能产生视觉调用费用。'
            : resuming
              ? '继续已选择的候选，后续分析和检查可能产生视觉调用费用。'
              : '本次将生成候选图片。选择候选后，再继续教学规划与检查。'}
      </p>
      <dl className="nmm-estimate">
        <div>
          <dt>当前动作</dt>
          <dd>
            {cachedRecovery
              ? '读取已保存结果，回到候选确认；当前预计 0 次新图片和 0 次视觉调用'
              : visionOnly
                ? '不生成新图片；教学规划、细化与质检最多 4 次视觉调用'
                : '1 次图片生成；全流程视觉调用最坏上限 5 次（含最多 1 次首次分析）'}
          </dd>
        </div>
        <div>
          <dt>项目生成余量</dt>
          <dd>
            {estimate.remaining_generations} / {estimate.generation_limit}
          </dd>
        </div>
      </dl>
      {estimate.current_config?.map((config, i) => (
        <p className="nmm-model" key={i}>
          {config.provider} · <strong>{config.model || '未指定模型'}</strong>
          {config.region ? ` · ${config.region}` : ''}
        </p>
      ))}
      {live ? (
        <>
          <p className="nmm-warning">
            原图、效果或配色参考及文字要求可能发送到所配置的外部模型服务；教学阶段会发送生成的效果与步骤图。过程参考仅保存在本地。总费用未知，次数上限不是金额上限。
          </p>
          {cachedRecovery && (
            <p className="nmm-hint">
              当前账本要求正数调用授权。默认仅批准 1
              次后续视觉额度；恢复缓存本身不会消耗这次额度。
            </p>
          )}
          {visionOnly && visionCap === 0 && (
            <p className="nmm-warning">
              视觉额度已耗尽，当前账本无法建立正数授权。请保留现有结果或新建项目；不会绕过额度限制继续。
            </p>
          )}
          <div className="nmm-budget">
            <label>
              本次图片调用上限
              <input
                type="number"
                min={0}
                max={imageCap}
                value={images}
                onChange={(e) => setImages(Number(e.target.value))}
              />
            </label>
            <label>
              本次视觉调用上限
              <input
                type="number"
                min={0}
                max={visionCap}
                value={vision}
                onChange={(e) => setVision(Number(e.target.value))}
              />
            </label>
          </div>
          <p className="nmm-hint">
            项目剩余：图片 {imageCap} 次，视觉 {visionCap}{' '}
            次。后续操作会再次确认，不会自动追加额度。
          </p>
          <label className="nmm-check">
            <input
              type="checkbox"
              checked={accepted}
              onChange={(e) => setAccepted(e.target.checked)}
            />
            我接受总费用未知，并同意本次数据发送和调用次数上限
          </label>
        </>
      ) : (
        <p>当前是显式注入的离线测试模式，不会调用真实模型。</p>
      )}
      <div className="nmm-actions">
        <button onClick={onClose}>暂不继续</button>
        <button
          className="primary-button"
          disabled={live && (!accepted || !valid || !estimate.estimate_id)}
          onClick={() =>
            onApprove(
              live
                ? {
                    estimate_id: estimate.estimate_id!,
                    max_image_calls: images,
                    max_vision_calls: vision,
                    accept_unknown_cost: true,
                  }
                : undefined,
            )
          }
        >
          批准并继续
        </button>
      </div>
    </NmmDialog>
  )
}
