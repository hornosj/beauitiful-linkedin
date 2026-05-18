import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import SearchForm from '../src/renderer/src/components/SearchForm'
import { emptyFormState, type SearchFormState } from '../src/shared/validation'

function renderSearchForm(form: SearchFormState) {
  return render(
    <SearchForm
      form={form}
      errors={{}}
      rolePresets={[]}
      seniority={[]}
      scrapeModes={[]}
      onChange={() => undefined}
      onFilterChange={() => undefined}
      onRolePresetChange={() => undefined}
      onScrapeModeChange={() => undefined}
      onSubmit={vi.fn()}
      running={false}
    />
  )
}

describe('SearchForm', () => {
  it('hides search intensity and cards-per-cycle controls in people_search mode', () => {
    renderSearchForm({ ...emptyFormState, scrapeMode: 'people_search' })

    expect(screen.queryByText(/intensidade da busca/i)).toBeNull()
    expect(screen.queryByLabelText(/cards por clique/i)).toBeNull()
  })

  it('keeps search intensity visible for API searches', () => {
    renderSearchForm({ ...emptyFormState, scrapeMode: 'api' })

    expect(screen.getByText(/intensidade da busca/i)).toBeInTheDocument()
    expect(screen.queryByLabelText(/cards por clique/i)).toBeNull()
  })
})
