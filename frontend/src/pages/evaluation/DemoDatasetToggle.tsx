import type { Dataset } from '../../evaluationApi'

export function firstRealDatasetId(datasets: Dataset[]) {
  return datasets.find((dataset) => dataset.corpus === 'live')?.id || ''
}

export default function DemoDatasetToggle({
  checked,
  disabled,
  onChange,
}: {
  checked: boolean
  disabled?: boolean
  onChange: (checked: boolean) => void
}) {
  return (
    <label className="evaluation-demo-toggle">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
      />
      显示合成示例（历史）
    </label>
  )
}
