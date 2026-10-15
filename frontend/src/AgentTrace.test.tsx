import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import AgentTrace from './components/AgentTrace'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

it('loads the tool steps only when expanded and flags invalid citations', async () => {
  const fetchMock = vi.fn(async () =>
    Response.json({
      run_id: 3,
      steps: [
        {
          kind: 'search',
          query: 'NMM 原理',
          strategy: 'rerank',
          keys: ['technique:7'],
        },
        { kind: 'search', query: '税率', strategy: 'rerank', keys: [] },
        {
          kind: 'research',
          task: '金色 NMM',
          searches: 2,
          covered: ['原理'],
          missing: ['税率'],
        },
        { kind: 'invalid_citations', keys: ['technique:9'] },
      ],
    }),
  )
  vi.stubGlobal('fetch', fetchMock)
  render(<AgentTrace runId={3} />)
  expect(fetchMock).not.toHaveBeenCalled()
  await userEvent.click(screen.getByText('Agent 检索过程'))
  expect(
    await screen.findByText(/检索“NMM 原理” → technique:7/),
  ).toBeInTheDocument()
  expect(screen.getByText(/检索“税率” → 无结果/)).toBeInTheDocument()
  expect(screen.getByText(/未找到依据：税率/)).toBeInTheDocument()
  expect(screen.getByText(/technique:9，请勿采信/)).toBeInTheDocument()
  expect(fetchMock).toHaveBeenCalledWith(
    '/api/v1/runs/3/trace',
    expect.anything(),
  )
})
