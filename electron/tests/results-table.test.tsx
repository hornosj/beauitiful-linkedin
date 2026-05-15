import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import ResultsTable from '../src/renderer/src/components/ResultsTable'
import type { Lead, ProspectingSummary } from '../src/shared/types'

const lead: Lead = {
  company_name: 'Nubank',
  person_name: 'Ana Silva',
  title: 'Head of Marketing',
  linkedin_url: 'https://www.linkedin.com/in/ana-silva/',
  source_url: 'https://www.linkedin.com/in/ana-silva/',
  source_type: 'linkedin_people_search',
  snippet: '',
  matched_title: 'marketing',
  confidence_score: 88
}

const summary: ProspectingSummary = {
  total_companies_processed: 1,
  total_raw_leads: 1,
  total_deduplicated_leads: 1,
  total_previously_consulted_leads: 0,
  total_maybe_incorrect_leads: 0,
  output_file: 'output/leads.csv',
  top_sources: { linkedin_people_search: 1 }
}

describe('ResultsTable', () => {
  it('exposes a save-current-search action beside the current search results', () => {
    const onSaveCurrent = vi.fn()

    render(
      <ResultsTable
        leads={[lead]}
        total={1}
        summary={summary}
        query=""
        onQueryChange={() => undefined}
        filter="all"
        onFilterChange={() => undefined}
        onSaveCurrent={onSaveCurrent}
      />
    )

    fireEvent.click(screen.getByRole('button', { name: /salvar busca atual/i }))
    fireEvent.change(screen.getByLabelText(/nome da tabela salva/i), {
      target: { value: 'Nubank Marketing' }
    })
    fireEvent.click(screen.getByRole('button', { name: /^salvar$/i }))

    expect(onSaveCurrent).toHaveBeenCalledWith('Nubank Marketing')
  })
})
