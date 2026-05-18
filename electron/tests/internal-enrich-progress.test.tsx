import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import InternalEnrichProgress from '../src/renderer/src/components/InternalEnrichProgress'

const baseProps = {
  open: true,
  totalLeads: 2,
  running: true,
  phase: 'validating' as const,
  completed: 0,
  domainsDone: 2,
  uniqueDomains: 2,
  recentLeads: [],
  summary: null,
  errorMessage: null,
  onCancel: vi.fn(),
  onClose: vi.fn()
}

describe('InternalEnrichProgress', () => {
  it('minimizes to background when the running modal backdrop is clicked', () => {
    const onCancel = vi.fn()
    const onClose = vi.fn()
    const { container } = render(
      <InternalEnrichProgress
        {...baseProps}
        onCancel={onCancel}
        onClose={onClose}
      />
    )

    fireEvent.click(container.querySelector('.enrich-overlay-backdrop')!)

    expect(onClose).toHaveBeenCalledOnce()
    expect(onCancel).not.toHaveBeenCalled()
  })

  it('keeps the explicit cancel button as the only cancel action while running', () => {
    const onCancel = vi.fn()
    const onClose = vi.fn()
    render(
      <InternalEnrichProgress
        {...baseProps}
        onCancel={onCancel}
        onClose={onClose}
      />
    )

    fireEvent.click(screen.getByRole('button', { name: 'Cancelar' }))

    expect(onCancel).toHaveBeenCalledOnce()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('highlights each found email in the live modal feed', () => {
    const { container } = render(
      <InternalEnrichProgress
        {...baseProps}
        recentLeads={[
          {
            type: 'lead',
            lead_ref: 'ana',
            person_name: 'Ana Silva',
            company_name: 'Nubank',
            status: 'enriched',
            email: 'ana@nubank.com.br',
            confidence: 95,
            chosen_domain: 'nubank.com.br',
            tested_domains: ['nubank.com.br']
          }
        ]}
      />
    )

    expect(screen.getByText('ana@nubank.com.br')).toBeInTheDocument()
    expect(screen.getByText('Encontrado')).toBeInTheDocument()
    expect(container.querySelector('.enrich-lead-row[data-status="enriched"]')).toBeTruthy()
  })
})
