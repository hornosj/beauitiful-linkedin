import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { ApiClient } from '../src/shared/api'
import type { Lead, SavedLeadTable, SavedLeadTableDetail } from '../src/shared/types'
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
  title: 'Head of Growth',
  linkedin_url: 'https://www.linkedin.com/in/ana/',
  email: null,
  phone: null,
  source_url: 'https://www.linkedin.com/in/ana/',
  source_type: 'linkedin',
  snippet: '',
  confidence_score: 95
}

const detail: SavedLeadTableDetail = { table, leads: [lead] }

describe('SavedLeadsLibrary Telegram phone enrichment', () => {
  // Único caminho de telefone após a reforma: o usuário clica em
  // "Achar telefone" dentro do painel de evidências (já listou CPFs via
  // pipeline) e o backend roda `telegramPhoneTelethonCpfStage`. Sem
  // Chrome, sem Playwright — nenhum `startTelegramPhone`/`runCpfStage`
  // deve ser invocado neste fluxo.
  it('runs the per-consult "Achar telefone" button through Telethon CPF stage', async () => {
    const nameConsult = {
      id: 1,
      table_id: 'table-1',
      lead_ref: lead.linkedin_url,
      provider: 'gon',
      lead_name: 'Ana Silva',
      query: '/nome Ana Silva',
      raw_text: 'Nome: Ana Silva\nCPF: 111.222.333-44',
      source_url: 'https://t.me/ConsultoriaGonzalesbot',
      downloaded_at: '2026-05-22T10:00:00Z',
      error: null,
      extracted_nome: 'Ana Silva',
      extracted_cpf: '111.222.333-44',
      extracted_birth_date: '10/01/1985',
      extracted_address: null,
      extracted_candidates: [],
      match_score: 90,
      match_details: {},
      created_at: '2026-05-22T10:00:00Z',
      run_id: null,
      query_type: 'name',
      query_value: 'Ana Silva',
      blocked_reason: null
    }
    const cpfConsult = {
      ...nameConsult,
      id: 2,
      provider: 'gon_cpf',
      query: '/cpf 111.222.333-44',
      raw_text: 'Telefone: (11) 99999-0000',
      query_type: 'cpf',
      query_value: '111.222.333-44'
    }
    const telegramPhoneTelethonCpfStage = vi.fn().mockResolvedValue({
      status: 'completed',
      summary: {
        requested_leads: 1,
        name_consults: 0,
        cpf_consults: 1,
        leads_with_phone: 1,
        phones_persisted: 1
      },
      leads: [
        {
          lead_ref: lead.linkedin_url,
          lead_name: 'Ana Silva',
          name_consults: [],
          cpf_consults: [cpfConsult],
          blocked_reason: null,
          candidates: [
            {
              phone_raw: '(11) 99999-0000',
              phone_digits: '11999990000',
              cpf: '111.222.333-44',
              confidence: 90,
              source_provider: 'gon_cpf',
              nome: 'Ana Silva',
              provenance: {}
            }
          ]
        }
      ]
    })
    const client = {
      listLeadTables: vi.fn().mockResolvedValue([table]),
      getLeadTable: vi.fn().mockResolvedValue(detail),
      getEnrichmentPricing: vi.fn().mockResolvedValue({ items: [], currency: 'BRL', note: '' }),
      listTelegramConsults: vi.fn().mockResolvedValue({ consults: [nameConsult] }),
      telegramPhoneTelethonCpfStage
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
    fireEvent.click(await screen.findByRole('button', { name: /Telegram · 1/i }))
    fireEvent.click(await screen.findByRole('button', { name: 'Achar telefone' }))

    await waitFor(() => {
      expect(telegramPhoneTelethonCpfStage).toHaveBeenCalledWith('table-1', {
        lead_ref: lead.linkedin_url,
        cpf: '111.222.333-44'
      })
    })
  })

  it('offers a Telethon retry when saved evidence has no CPF', async () => {
    const noCpfConsult = {
      id: 3,
      table_id: 'table-1',
      lead_ref: lead.linkedin_url,
      provider: 'gon',
      lead_name: 'Ana Silva',
      query: '/nome Ana Silva',
      raw_text: 'Nenhum CPF encontrado.',
      source_url: null,
      downloaded_at: '2026-05-22T10:00:00Z',
      error: null,
      extracted_nome: null,
      extracted_cpf: null,
      extracted_birth_date: null,
      extracted_address: null,
      extracted_candidates: [],
      match_score: null,
      match_details: {},
      created_at: '2026-05-22T10:00:00Z',
      run_id: null,
      query_type: 'name',
      query_value: 'Ana Silva',
      blocked_reason: 'no_cpf_from_name_stage'
    }
    const telegramConsultTelethonExperimental = vi.fn().mockResolvedValue({
      summary: { requested_leads: 1, succeeded: 1, failed: 0 },
      consults: [noCpfConsult]
    })
    const client = {
      listLeadTables: vi.fn().mockResolvedValue([table]),
      getLeadTable: vi.fn().mockResolvedValue(detail),
      getEnrichmentPricing: vi.fn().mockResolvedValue({ items: [], currency: 'BRL', note: '' }),
      listTelegramConsults: vi.fn().mockResolvedValue({ consults: [noCpfConsult] }),
      getTelethonAuthStatus: vi
        .fn()
        .mockResolvedValue({ configured: true, authorized: true, session_name: 'beautiful' }),
      telegramConsultTelethonExperimental
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
    fireEvent.click(await screen.findByRole('button', { name: /Telegram · 1/i }))
    fireEvent.click(await screen.findByRole('button', { name: 'Tentar novamente' }))

    await waitFor(() => {
      expect(telegramConsultTelethonExperimental).toHaveBeenCalledWith('table-1', {
        lead_refs: [lead.linkedin_url],
        max_leads: 10
      })
    })
    const log = await screen.findByText('Trilha da consulta')
    const logText = log.closest('[role="status"]')?.textContent ?? ''
    expect(logText).toContain('Preparando sessão Telegram')
    expect(logText).toContain('Consultando dados via sessão nativa')
    expect(logText).not.toContain('Finder')
    expect(logText).not.toContain('Gonzales')
    expect(logText).not.toContain('Unix')
  })

  // Fluxo principal de extração de CPFs: usuário seleciona leads e clica
  // "Extrair CPFs via Telegram". Pipeline roda só via Telethon (nenhum
  // Chrome bootstrap), respeita auth status, dispara o endpoint correto.
  it('runs the Telethon pipeline without touching Chrome', async () => {
    const telegramConsultTelethonExperimental = vi.fn().mockResolvedValue({
      summary: { requested_leads: 1, succeeded: 1, failed: 0 },
      consults: []
    })
    const telegramConsultTelethonPipeline = vi.fn().mockResolvedValue({
      status: 'completed',
      summary: {
        requested_leads: 1,
        name_consults: 3,
        cpf_consults: 1,
        leads_with_phone: 1,
        phones_persisted: 1
      },
      leads: [
        {
          lead_ref: lead.linkedin_url,
          lead_name: 'Ana Silva',
          name_consults: [],
          cpf_consults: [],
          blocked_reason: null,
          candidates: []
        }
      ]
    })
    const onFeedback = vi.fn()
    const client = {
      listLeadTables: vi.fn().mockResolvedValue([table]),
      getLeadTable: vi.fn().mockResolvedValue(detail),
      getEnrichmentPricing: vi.fn().mockResolvedValue({ items: [], currency: 'BRL', note: '' }),
      listTelegramConsults: vi.fn().mockResolvedValue({ consults: [] }),
      telegramConsultTelethonExperimental,
      telegramConsultTelethonPipeline,
      getTelethonAuthStatus: vi
        .fn()
        .mockResolvedValue({ configured: true, authorized: true, session_name: 'beautiful' })
    } as unknown as ApiClient

    render(
      <EnrichmentRunnerProvider client={client}>
        <SavedLeadsLibrary
          client={client}
          currentLeads={[]}
          currentKeywords={[]}
          currentSearchRequest={null}
          onFeedback={onFeedback}
        />
      </EnrichmentRunnerProvider>
    )

    await screen.findByText('1 tabelas salvas')
    fireEvent.click(screen.getAllByRole('button', { name: /Nubank/i })[1])
    fireEvent.click(await screen.findByRole('checkbox', { name: /Selecionar Ana Silva/i }))
    fireEvent.click(
      await screen.findByRole('button', { name: /Extrair CPFs via Telegram/i })
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Rodar pipeline' }))

    await waitFor(() => {
      expect(telegramConsultTelethonExperimental).toHaveBeenCalledWith('table-1', {
        lead_refs: [lead.linkedin_url],
        max_leads: 10
      })
    })
    expect(telegramConsultTelethonPipeline).not.toHaveBeenCalled()
    expect(onFeedback).toHaveBeenCalledWith(
      'success',
      expect.stringContaining('Consulta de CPF concluída')
    )
  })

  it('runs "Encontrar telefones" through the joined Telethon flow using only the top CPF', async () => {
    const leadB: Lead = {
      ...lead,
      person_name: 'Bia Costa',
      linkedin_url: 'https://www.linkedin.com/in/bia/',
      source_url: 'https://www.linkedin.com/in/bia/'
    }
    const leadC: Lead = {
      ...lead,
      person_name: 'Caio Lima',
      linkedin_url: 'https://www.linkedin.com/in/caio/',
      source_url: 'https://www.linkedin.com/in/caio/'
    }
    const multiDetail: SavedLeadTableDetail = {
      table: { ...table, lead_count: 3 },
      leads: [lead, leadB, leadC]
    }
    const telegramConsultTelethonPipeline = vi.fn().mockResolvedValue({
      status: 'completed',
      summary: {
        requested_leads: 3,
        name_consults: 3,
        cpf_consults: 3,
        leads_with_phone: 1,
        phones_persisted: 1
      },
      leads: [
        {
          lead_ref: lead.linkedin_url,
          lead_name: 'Ana Silva',
          name_consults: [],
          cpf_consults: [],
          blocked_reason: null,
          candidates: []
        }
      ]
    })
    const client = {
      listLeadTables: vi.fn().mockResolvedValue([table]),
      getLeadTable: vi.fn().mockResolvedValue(multiDetail),
      getEnrichmentPricing: vi.fn().mockResolvedValue({ items: [], currency: 'BRL', note: '' }),
      listTelegramConsults: vi.fn().mockResolvedValue({ consults: [] }),
      telegramConsultTelethonPipeline,
      getTelethonAuthStatus: vi
        .fn()
        .mockResolvedValue({ configured: true, authorized: true, session_name: 'beautiful' })
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
    const leadCheckboxes = await screen.findAllByRole('checkbox', { name: /Selecionar/i })
    for (const checkbox of leadCheckboxes) fireEvent.click(checkbox)
    fireEvent.click(await screen.findByRole('button', { name: /Encontrar telefones/i }))
    expect(await screen.findByText(/Tempo estimado: cerca de 3 min ou mais/i)).toBeTruthy()
    fireEvent.click(await screen.findByRole('button', { name: 'Encontrar telefones' }))

    await waitFor(() => {
      expect(telegramConsultTelethonPipeline).toHaveBeenCalledWith('table-1', {
        lead_refs: [lead.linkedin_url, leadB.linkedin_url, leadC.linkedin_url],
        max_leads: 10,
        max_cpf_candidates: 1
      })
    })
  })
})
