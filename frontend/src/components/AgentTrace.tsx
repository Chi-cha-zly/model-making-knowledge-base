import { useState } from 'react'
import { chatApi, type TraceStep } from '../chatApi'

function describe(step: TraceStep) {
  switch (step.kind) {
    case 'research':
      return `检索助手任务：${step.task}（检索 ${step.searches} 次${
        step.covered.length ? `；已覆盖：${step.covered.join('、')}` : ''
      }${step.missing.length ? `；未找到依据：${step.missing.join('、')}` : ''}）`
    case 'search':
      return `检索“${step.query}” → ${
        step.keys.length ? step.keys.join('、') : '无结果'
      }`
    case 'read':
      return `读取 ${step.key} 全文${step.found ? '' : '（不可用）'}`
    case 'invalid_citations':
      return `注意：回答引用了不在本轮证据中的编号 ${step.keys.join('、')}，请勿采信这些引用`
  }
}

export default function AgentTrace({ runId }: { runId: number }) {
  const [steps, setSteps] = useState<TraceStep[] | null>(null)
  const [error, setError] = useState('')
  const load = () => {
    if (steps !== null) return
    chatApi
      .trace(runId)
      .then((trace) => setSteps(trace.steps))
      .catch((reason: unknown) =>
        setError(reason instanceof Error ? reason.message : '加载失败'),
      )
  }
  return (
    <details className="agent-trace" onToggle={load}>
      <summary>Agent 检索过程</summary>
      {error && <p className="chat-run-error">{error}</p>}
      {steps === null && !error && <p className="chat-muted">加载中…</p>}
      {steps?.length === 0 && (
        <p className="chat-muted">本轮没有调用工具，直接回答。</p>
      )}
      {!!steps?.length && (
        <ol>
          {steps.map((step, index) => (
            <li key={index} className={`trace-${step.kind}`}>
              {describe(step)}
            </li>
          ))}
        </ol>
      )}
    </details>
  )
}
