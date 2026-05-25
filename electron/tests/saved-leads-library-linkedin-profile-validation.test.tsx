import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ApiClient } from '../src/shared/api'
import type { Lead, SavedLeadTable, SavedLeadTableDetail } from '../src/shared/types'

const chromeBootstrapModal = vi.hoisted(() => ({
  latestProps: null as null | {
    targetUrl: string
    targetSlug: string
    targetKind?: string
    onReady(): void
  }
}))

vi.mock('../src/renderer/src/components/ChromeBootstrapModal', async () => {
  const React = await vi.importActual<typeof import('react')>('react')
  return {
    default: (props: {
      targetUrl: string
      targetSlug: string
      targetKind?: string
      onReady(): void
    }) => {
      chromeBootstrapModal.latestProps = props
      React.useEffect(() => {
        props.onReady()
      }, [])
      return React.createElement('div', {
        role: 'dialog',
        'aria-label': 'Preparando Chrome mock'
      })
    }
  }
})

import SavedLeadsLibrary from '../src/renderer/src/components/SavedLeadsLibrary'
import { EnrichmentRunnerProvider } from '../src/renderer/src/enrichment/EnrichmentRunnerContext'

class ResizeObserverMock {
  observe = vi.fn()
  unobserve = vi.fn()
  disconnect = vi.fn()
}

globalThis.ResizeObserver = ResizeObserverMock as unknown as typeof ResizeObserver

const table: SavedLeadTable = {
  id: 'table-1',
  name: 'Nubank',
  created_at: '2026-05-20T10:00:00Z',
  updated_at: '2026-05-20T10:00:00Z',
  source_type: 'search',
  keywords: ['growth'],
  search_queries: [],
  search_request: { company_domain: 'nubank.com.br' },
  enrichment_status: 'not_enriched',
  lead_count: 1
}

const lead: Lead = {
  company_name: 'Nubank',
  company_domain: 'nubank.com.br',
  person_name: 'Ana Silva',
  title: 'Head of Marketing',
  linkedin_url: 'https://www.linkedin.com/in/ana-silva/',
  email: 'ana@nubank.com.br',
  phone: null,
  source_url: 'https://www.linkedin.com/in/ana-silva/',
  source_type: 'linkedin_people_search',
  snippet: '',
  confidence_score: 92
}

const validatedLead: Lead = {
  ...lead,
  linkedin_experience_title: 'Head of Growth',
  linkedin_experience_company: 'Nubank',
  linkedin_contact_email: 'ana@gmail.com',
  linkedin_contact_website: 'https://portfolio.example/ana'
}

const detail: SavedLeadTableDetail = { table, leads: [lead] }

describe('SavedLeadsLibrary LinkedIn profile validation', () => {
  beforeEach(() => {
    delete (window as unknown as { beautifulLinkedIn?: unknown }).beautifulLinkedIn
    chromeBootstrapModal.latestProps = null
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('validates selected leads and displays LinkedIn contact fields', async () => {
    const validateLinkedInProfiles = vi.fn().mockResolvedValue({
      status: 'completed',
      summary: { requested_leads: 1, validated_leads: 1, failed_leads: 0, no_change: 0 },
      table,
      leads: [validatedLead]
    })
    const client = {
      listLeadTables: vi.fn().mockResolvedValue([table]),
      getLeadTable: vi.fn().mockResolvedValue(detail),
      getEnrichmentPricing: vi.fn().mockResolvedValue({ items: [], currency: 'BRL', note: '' }),
      validateLinkedInProfiles
    } as unknown as ApiClient

    render(
      <EnrichmentRunnerProvider client={client}>
        <SavedLeadsLibrary
          client={client}
          currentLeads={[]}
          currentKeywords={[]}
          currentSearchRequest={null}
          onFeedback={vi.fn()}
        />
      </EnrichmentRunnerProvider>
    )

    await screen.findByText('1 tabelas salvas')
    fireEvent.click(screen.getAllByRole('button', { name: /Nubank/i })[1])
    fireEvent.click(await screen.findByLabelText('Selecionar Ana Silva'))

    fireEvent.click(screen.getByRole('button', { name: /Validar LinkedIn/i }))
    fireEvent.click(await screen.findByRole('button', { name: 'Validar LinkedIn' }))

    await waitFor(() => {
      expect(validateLinkedInProfiles).toHaveBeenCalledWith(
        'table-1',
        expect.objectContaining({
          lead_refs: ['https://www.linkedin.com/in/ana-silva/'],
          max_leads: 40
        })
      )
    })
    expect(await screen.findByText('Head of Growth')).toBeTruthy()
    expect(screen.getByText('ana@gmail.com')).toBeTruthy()
    expect(screen.getByText('portfolio.example')).toBeTruthy()
  })

  it('prepares the Chrome CDP window before validating profiles when CDP is offline', async () => {
    const validateLinkedInProfiles = vi.fn().mockResolvedValue({
      status: 'completed',
      summary: { requested_leads: 1, validated_leads: 1, failed_leads: 0, no_linkedin_url: 0, no_change: 0 },
      table,
      leads: [validatedLead]
    })
    const chrome = {
      probe: vi.fn().mockResolvedValue({ alive: false, endpoint: 'http://127.0.0.1:9222' }),
      launch: vi.fn().mockResolvedValue({ launched: true, executable: 'chrome.exe', error: null }),
      waitForCdp: vi.fn().mockResolvedValue(true),
      checkLinkedIn: vi.fn().mockResolvedValue({ state: 'logged_in', url: 'https://www.linkedin.com/feed/' }),
      openUrl: vi.fn().mockResolvedValue({ opened: true }),
      listTabs: vi
        .fn()
        .mockResolvedValue([{ url: 'https://www.linkedin.com/in/ana-silva/details/experience/' }])
    }
    ;(window as unknown as { beautifulLinkedIn: unknown }).beautifulLinkedIn = {
      chrome
    }
    const client = {
      listLeadTables: vi.fn().mockResolvedValue([table]),
      getLeadTable: vi.fn().mockResolvedValue(detail),
      getEnrichmentPricing: vi.fn().mockResolvedValue({ items: [], currency: 'BRL', note: '' }),
      validateLinkedInProfiles
    } as unknown as ApiClient

    render(
      <EnrichmentRunnerProvider client={client}>
        <SavedLeadsLibrary
          client={client}
          currentLeads={[]}
          currentKeywords={[]}
          currentSearchRequest={null}
          onFeedback={vi.fn()}
        />
      </EnrichmentRunnerProvider>
    )

    await screen.findByText('1 tabelas salvas')
    fireEvent.click(screen.getAllByRole('button', { name: /Nubank/i })[1])
    fireEvent.click(await screen.findByLabelText('Selecionar Ana Silva'))
    fireEvent.click(screen.getByRole('button', { name: /Validar LinkedIn/i }))
    fireEvent.click(await screen.findByRole('button', { name: 'Validar LinkedIn' }))

    await waitFor(() => {
      expect(chromeBootstrapModal.latestProps).toMatchObject({
        targetUrl: 'https://www.linkedin.com/in/ana-silva/details/experience/',
        targetSlug: '/in/ana-silva',
        targetKind: 'profile'
      })
      expect(validateLinkedInProfiles).toHaveBeenCalled()
    })
    expect(chrome.launch).not.toHaveBeenCalled()
  })

  it('cleans up scraped remnants from lead titles', async () => {
    const dirtyLead: Lead = {
      ...lead,
      person_name: 'Dirty Title Lead',
      title: 'Pular para conteúdo principal Head of Marketing -',
      linkedin_experience_title: 'Pular para o conteúdo principal Head of Growth',
      company_name: 'Nubank - Pular para conteúdo principal',
      linkedin_experience_company: 'Skip to main content - Nubank'
    }
    const dirtyDetail: SavedLeadTableDetail = { table, leads: [dirtyLead] }
    const client = {
      listLeadTables: vi.fn().mockResolvedValue([table]),
      getLeadTable: vi.fn().mockResolvedValue(dirtyDetail),
      getEnrichmentPricing: vi.fn().mockResolvedValue({ items: [], currency: 'BRL', note: '' })
    } as unknown as ApiClient

    render(
      <EnrichmentRunnerProvider client={client}>
        <SavedLeadsLibrary
          client={client}
          currentLeads={[]}
          currentKeywords={[]}
          currentSearchRequest={null}
          onFeedback={vi.fn()}
        />
      </EnrichmentRunnerProvider>
    )

    await screen.findByText('1 tabelas salvas')
    fireEvent.click(screen.getAllByRole('button', { name: /Nubank/i })[1])

    await screen.findByText('Head of Marketing')
    expect(screen.queryByText(/Pular para conteúdo principal/)).toBeNull()
    expect(screen.queryByText(/Skip to main content/)).toBeNull()
    expect(screen.queryByText(/Pular para o conteúdo principal/)).toBeNull()
    expect(screen.getByText('Head of Growth')).toBeTruthy()
  })
})
