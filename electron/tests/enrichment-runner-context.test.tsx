import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { ApiClient } from '../src/shared/api'
import {
  EnrichmentRunnerProvider,
  useEnrichmentRunner
} from '../src/renderer/src/enrichment/EnrichmentRunnerContext'

function RunnerHarness() {
  const runner = useEnrichmentRunner()
  return (
    <div>
      <span data-testid="modal-state">{runner.modalOpen ? 'open' : 'closed'}</span>
      <span data-testid="run-state">{runner.run?.running ? 'running' : 'idle'}</span>
      <button
        type="button"
        onClick={() =>
          runner.start({
            tableId: 'table-1',
            tableName: 'Tabela RH',
            totalLeads: 3
          })
        }
      >
        Start
      </button>
      <button type="button" onClick={runner.openModal}>
        Open modal
      </button>
    </div>
  )
}

describe('EnrichmentRunnerProvider', () => {
  it('starts internal enrichment minimized so navigation stays usable', () => {
    const streamInternalEnrich = vi.fn(() => new Promise(() => undefined))
    const client = { streamInternalEnrich } as unknown as ApiClient

    render(
      <EnrichmentRunnerProvider client={client}>
        <RunnerHarness />
      </EnrichmentRunnerProvider>
    )

    fireEvent.click(screen.getByRole('button', { name: 'Start' }))

    expect(screen.getByTestId('run-state')).toHaveTextContent('running')
    expect(screen.getByTestId('modal-state')).toHaveTextContent('closed')
    expect(streamInternalEnrich).toHaveBeenCalledOnce()
  })

  it('still lets the user open the progress modal from the background chip', () => {
    const streamInternalEnrich = vi.fn(() => new Promise(() => undefined))
    const client = { streamInternalEnrich } as unknown as ApiClient

    render(
      <EnrichmentRunnerProvider client={client}>
        <RunnerHarness />
      </EnrichmentRunnerProvider>
    )

    fireEvent.click(screen.getByRole('button', { name: 'Start' }))
    fireEvent.click(screen.getByRole('button', { name: 'Open modal' }))

    expect(screen.getByTestId('modal-state')).toHaveTextContent('open')
  })
})
