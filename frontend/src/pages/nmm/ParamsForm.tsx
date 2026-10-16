import type { NmmParams } from '../../nmmApi'
export const defaultParams: NmmParams = {
  metal: 'gold',
  light_direction: 'top_left',
  contrast: 'medium',
  step_count: 7,
  notes: '',
}
const metals = [
  ['gold', '金'],
  ['silver', '银'],
  ['steel', '钢'],
  ['copper', '铜'],
  ['bronze', '青铜'],
  ['rusted_iron', '锈铁'],
]
const lights = [
  ['top', '上方'],
  ['top_left', '左上'],
  ['top_right', '右上'],
  ['left', '左侧'],
  ['right', '右侧'],
  ['front', '正面'],
  ['bottom', '下方'],
  ['bottom_left', '左下'],
  ['bottom_right', '右下'],
  ['back', '背面'],
]
export default function ParamsForm({
  value,
  onChange,
  disabled = false,
}: {
  value: NmmParams
  onChange: (value: NmmParams) => void
  disabled?: boolean
}) {
  const custom = !metals.some(([key]) => key === value.metal)
  return (
    <fieldset className="nmm-params" disabled={disabled}>
      <legend>涂装参数</legend>
      <label>
        金属效果
        <select
          value={custom ? 'custom' : value.metal}
          onChange={(e) =>
            onChange({
              ...value,
              metal:
                e.target.value === 'custom' ? '自定义金属' : e.target.value,
            })
          }
        >
          {metals.map(([key, label]) => (
            <option key={key} value={key}>
              {label}
            </option>
          ))}
          <option value="custom">自定义</option>
        </select>
      </label>
      {custom && (
        <label>
          自定义金属
          <input
            maxLength={80}
            required
            value={value.metal}
            onChange={(e) => onChange({ ...value, metal: e.target.value })}
          />
        </label>
      )}
      <label>
        光源方向
        <select
          value={value.light_direction}
          onChange={(e) =>
            onChange({
              ...value,
              light_direction: e.target.value as NmmParams['light_direction'],
            })
          }
        >
          {lights.map(([key, label]) => (
            <option key={key} value={key}>
              {label}
            </option>
          ))}
        </select>
      </label>
      <label>
        对比度
        <select
          value={value.contrast}
          onChange={(e) =>
            onChange({
              ...value,
              contrast: e.target.value as NmmParams['contrast'],
            })
          }
        >
          <option value="low">柔和</option>
          <option value="medium">中等</option>
          <option value="high">强烈</option>
        </select>
      </label>
      <label>
        步骤数
        <input
          type="number"
          min={4}
          max={9}
          required
          value={value.step_count}
          onChange={(e) =>
            onChange({ ...value, step_count: Number(e.target.value) })
          }
        />
      </label>
      <label>
        补充要求
        <textarea
          maxLength={4000}
          rows={4}
          value={value.notes}
          onChange={(e) => onChange({ ...value, notes: e.target.value })}
        />
      </label>
    </fieldset>
  )
}
export function validParams(value: NmmParams) {
  return (
    !!value.metal.trim() &&
    value.metal.length <= 80 &&
    Number.isInteger(value.step_count) &&
    value.step_count >= 4 &&
    value.step_count <= 9 &&
    value.notes.length <= 4000
  )
}
