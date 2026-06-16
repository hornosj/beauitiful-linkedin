import { describe, expect, it } from 'vitest'
import {
  applyRolePresetToForm,
  buildSearchRequestFromForm,
  buildSearchRequestForCompany,
  collectCompanies,
  isRiskyScrapeMode,
  validateSearchForm,
  type SearchFormState
} from '../src/shared/validation'
import type { TaxonomyItem } from '../src/shared/types'

const baseForm: SearchFormState = {
  companyName: 'Nubank',
  companyDomain: 'nubank.com.br',
  linkedinUrl: 'https://www.linkedin.com/company/nubank/',
  extraCompanies: [],
  sameMaxForAll: true,
  tableMode: 'single',
  rolePreset: 'custom',
  titles: 'marketing, growth',
  generalSearch: false,
  maxResults: 25,
  cardsPerCycle: 10,
  scrapeMode: 'serp',
  searchDepth: 'standard',
  outputPath: 'output/leads.csv',
  includeUncertain: false,
  acceptRisk: false,
  filters: {
    seniority: ['c_level'],
    functions: ['marketing'],
    locations: '',
    excludeTitles: 'recruiter',
    minConfidence: '60',
    dropUnclassified: false
  }
}

describe('validateSearchForm', () => {
  it('passes for a well-formed form', () => {
    const result = validateSearchForm(baseForm)
    expect(result.ok).toBe(true)
  })

  it('reports missing company name', () => {
    const result = validateSearchForm({ ...baseForm, companyName: '   ' })
    expect(result.ok).toBe(false)
    if (!result.ok) {
      expect(result.errors.companyName).toBeTruthy()
    }
  })

  it('reports missing titles', () => {
    const result = validateSearchForm({ ...baseForm, titles: '' })
    expect(result.ok).toBe(false)
    if (!result.ok) {
      expect(result.errors.titles).toBeTruthy()
    }
  })

  it('requires acceptRisk on browser mode', () => {
    const result = validateSearchForm({
      ...baseForm,
      scrapeMode: 'browser',
      acceptRisk: false
    })
    expect(result.ok).toBe(false)
    if (!result.ok) {
      expect(result.errors.acceptRisk).toMatch(/risco/i)
    }
  })

  it('passes browser mode when risk is accepted', () => {
    const result = validateSearchForm({
      ...baseForm,
      scrapeMode: 'browser',
      acceptRisk: true
    })
    expect(result.ok).toBe(true)
  })
})

describe('collectCompanies (busca múltipla com domínio por empresa)', () => {
  it('carrega o domínio próprio de cada empresa extra', () => {
    const companies = collectCompanies({
      ...baseForm,
      companyName: 'Nubank',
      companyDomain: 'nubank.com.br',
      linkedinUrl: '',
      maxResults: 25,
      extraCompanies: [
        { name: 'Mercado Livre', domain: 'mercadolivre.com', linkedinUrl: '', maxResults: 0 },
        { name: 'Stone', domain: 'stone.com.br', linkedinUrl: '', maxResults: 0 }
      ]
    })
    expect(companies).toEqual([
      { name: 'Nubank', domain: 'nubank.com.br', maxResults: 25 },
      { name: 'Mercado Livre', domain: 'mercadolivre.com', maxResults: 25 },
      { name: 'Stone', domain: 'stone.com.br', maxResults: 25 }
    ])
  })

  it('cada empresa vira um request com seu próprio company_domain', () => {
    const form: SearchFormState = {
      ...baseForm,
      companyName: 'Nubank',
      companyDomain: 'nubank.com.br',
      linkedinUrl: '',
      extraCompanies: [
        { name: 'Mercado Livre', domain: 'mercadolivre.com', linkedinUrl: '', maxResults: 0 }
      ]
    }
    const [primary, extra] = collectCompanies(form)
    expect(buildSearchRequestForCompany(form, primary).company_domain).toBe('nubank.com.br')
    expect(buildSearchRequestForCompany(form, extra).company_domain).toBe('mercadolivre.com')
  })

  it('empresa extra sem domínio fica sem company_domain (não herda da primária)', () => {
    const form: SearchFormState = {
      ...baseForm,
      companyName: 'Nubank',
      companyDomain: 'nubank.com.br',
      linkedinUrl: '',
      extraCompanies: [{ name: 'Stone', domain: '   ', linkedinUrl: '', maxResults: 0 }]
    }
    const [, extra] = collectCompanies(form)
    expect(extra.domain).toBeUndefined()
    expect(buildSearchRequestForCompany(form, extra).company_domain).toBeUndefined()
  })

  it('usa a URL da aba People por empresa para o request (people_search)', () => {
    const peopleUrl = 'https://www.linkedin.com/company/mercadolivre-com/people/'
    const form: SearchFormState = {
      ...baseForm,
      companyName: 'Nubank',
      extraCompanies: [
        { name: 'Mercado Livre', domain: 'mercadolivre.com', linkedinUrl: peopleUrl, maxResults: 0 }
      ]
    }
    const [, extra] = collectCompanies(form)
    expect(extra.linkedinUrl).toBe(peopleUrl)
    expect(buildSearchRequestForCompany(form, extra).linkedin_url).toBe(peopleUrl)
  })

  it('aceita URL do LinkedIn colada no campo nome quando não há URL explícita', () => {
    const peopleUrl = 'https://www.linkedin.com/company/stone-co/people/'
    const [extra] = collectCompanies({
      ...baseForm,
      companyName: '',
      companyDomain: '',
      linkedinUrl: '',
      extraCompanies: [{ name: peopleUrl, domain: 'stone.com.br', linkedinUrl: '', maxResults: 0 }]
    })
    expect(extra.linkedinUrl).toBe(peopleUrl)
    expect(extra.domain).toBe('stone.com.br')
  })

  it('sameMaxForAll=true: toda empresa usa o máx. global', () => {
    const form: SearchFormState = {
      ...baseForm,
      companyName: 'Nubank',
      maxResults: 30,
      sameMaxForAll: true,
      extraCompanies: [
        { name: 'Stone', domain: 'stone.com.br', linkedinUrl: '', maxResults: 5 }
      ]
    }
    const [primary, extra] = collectCompanies(form)
    expect(buildSearchRequestForCompany(form, primary).max_results).toBe(30)
    expect(buildSearchRequestForCompany(form, extra).max_results).toBe(30)
  })

  it('sameMaxForAll=false: cada empresa extra usa seu próprio máx. (e cai no global se vazio)', () => {
    const form: SearchFormState = {
      ...baseForm,
      companyName: 'Nubank',
      maxResults: 30,
      sameMaxForAll: false,
      extraCompanies: [
        { name: 'Stone', domain: 'stone.com.br', linkedinUrl: '', maxResults: 5 },
        { name: 'iFood', domain: 'ifood.com.br', linkedinUrl: '', maxResults: 0 }
      ]
    }
    const [primary, stone, ifood] = collectCompanies(form)
    expect(buildSearchRequestForCompany(form, primary).max_results).toBe(30)
    expect(buildSearchRequestForCompany(form, stone).max_results).toBe(5)
    expect(buildSearchRequestForCompany(form, ifood).max_results).toBe(30)
  })
})

describe('buildSearchRequestFromForm', () => {
  it('strips empty optional fields and parses titles into array', () => {
    const request = buildSearchRequestFromForm({
      ...baseForm,
      companyDomain: '',
      linkedinUrl: '',
      titles: 'marketing, growth, '
    })
    expect(request.titles).toEqual(['marketing', 'growth'])
    expect(request.company_domain).toBeUndefined()
    expect(request.linkedin_url).toBeUndefined()
  })

  it('serializes filters into payload and skips empty min_confidence', () => {
    const request = buildSearchRequestFromForm({
      ...baseForm,
      filters: { ...baseForm.filters, minConfidence: '' }
    })
    expect(request.filters).toMatchObject({
      seniority_in: ['c_level'],
      functions_in: ['marketing'],
      exclude_titles: ['recruiter']
    })
    expect(request.filters?.min_confidence_score).toBeNull()
  })

  it('forwards accept_risk flag for browser mode', () => {
    const request = buildSearchRequestFromForm({
      ...baseForm,
      scrapeMode: 'browser',
      acceptRisk: true
    })
    expect(request.scrape_mode).toBe('browser')
    expect(request.accept_risk).toBe(true)
  })

  it('forwards the selected search depth', () => {
    const request = buildSearchRequestFromForm({
      ...baseForm,
      searchDepth: 'deep'
    })
    expect(request.search_depth).toBe('deep')
  })

  it('serializes configured API keys and drops blank values', () => {
    const request = buildSearchRequestFromForm(baseForm, {
      apollo_api_key: '  apollo-test-key  ',
      serper_api_key: '',
      linkedin_cookie_browser: 'chrome'
    })
    expect(request.api_keys).toEqual({
      apollo_api_key: 'apollo-test-key',
      linkedin_cookie_browser: 'chrome'
    })
  })
})

describe('applyRolePresetToForm', () => {
  const marketingPreset: TaxonomyItem = {
    value: 'marketing_growth',
    label: 'Marketing / Growth',
    aliases: ['marketing', 'growth', 'head of marketing']
  }

  it('fills titles from the selected preset keywords', () => {
    const next = applyRolePresetToForm(baseForm, marketingPreset)
    expect(next.rolePreset).toBe('marketing_growth')
    expect(next.titles).toBe('marketing, growth, head of marketing')
  })

  it('keeps manual titles when custom mode is selected', () => {
    const next = applyRolePresetToForm(baseForm, null)
    expect(next.rolePreset).toBe('custom')
    expect(next.titles).toBe(baseForm.titles)
  })
})

describe('general search mode', () => {
  it('accepts empty titles when generalSearch=true', () => {
    const form: SearchFormState = { ...baseForm, titles: '', generalSearch: true }
    const result = validateSearchForm(form)
    expect(result.ok).toBe(true)
  })

  it('rejects empty titles when generalSearch=false', () => {
    const form: SearchFormState = { ...baseForm, titles: '', generalSearch: false }
    const result = validateSearchForm(form)
    expect(result.ok).toBe(false)
    if (!result.ok) {
      expect(result.errors.titles).toBeTruthy()
    }
  })

  it('serializes empty titles + general_search=true into the request', async () => {
    const mod = await import('../src/shared/validation')
    const form: SearchFormState = {
      ...baseForm,
      titles: 'marketing, growth',
      generalSearch: true
    }
    const request = mod.buildSearchRequestFromForm(form)
    expect(request.titles).toEqual([])
    expect(request.general_search).toBe(true)
  })

  it('serializes titles when not in general mode', async () => {
    const mod = await import('../src/shared/validation')
    const form: SearchFormState = {
      ...baseForm,
      titles: 'marketing, growth',
      generalSearch: false
    }
    const request = mod.buildSearchRequestFromForm(form)
    expect(request.titles).toEqual(['marketing', 'growth'])
    expect(request.general_search).toBe(false)
  })
})

describe('default form state', () => {
  it('defaults to people_search to match the simplified UI', async () => {
    const mod = await import('../src/shared/validation')
    expect(mod.emptyFormState.scrapeMode).toBe('people_search')
  })

  it('uses 8 cards per cycle by default for people_search', async () => {
    const mod = await import('../src/shared/validation')
    expect(mod.emptyFormState.cardsPerCycle).toBe(8)
    expect(mod.buildSearchRequestFromForm(mod.emptyFormState).cards_per_cycle).toBe(8)
  })
})

describe('isRiskyScrapeMode', () => {
  it('flags only the browser mode', () => {
    expect(isRiskyScrapeMode('browser')).toBe(true)
    expect(isRiskyScrapeMode('serp')).toBe(false)
    expect(isRiskyScrapeMode('cookie')).toBe(false)
    expect(isRiskyScrapeMode('api')).toBe(false)
    expect(isRiskyScrapeMode('people_search')).toBe(false)
  })
})
