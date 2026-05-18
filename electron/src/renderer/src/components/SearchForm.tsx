import type { ScrapeMode, SearchRequest, Seniority, TaxonomyItem } from '../../../shared/types'
import type { SearchFormFilters, SearchFormState, ValidationErrors } from '../../../shared/validation'
import { isRiskyScrapeMode } from '../../../shared/validation'

interface Props {
  form: SearchFormState
  errors: ValidationErrors
  rolePresets: TaxonomyItem[]
  seniority: TaxonomyItem[]
  scrapeModes: TaxonomyItem[]
  onChange(patch: Partial<SearchFormState>): void
  onFilterChange(patch: Partial<SearchFormFilters>): void
  onRolePresetChange(value: string): void
  onScrapeModeChange(mode: ScrapeMode): void
  onSubmit(event: React.FormEvent): void
  running: boolean
}

const FALLBACK_MODES: TaxonomyItem[] = [
  { value: 'serp', label: 'Busca pública (sem conta)' },
  { value: 'api', label: 'APIs externas + busca pública' },
  { value: 'cookie', label: 'LinkedIn com cookie (li_at)' },
  { value: 'people_search', label: 'LinkedIn People (li_at, sem visitar perfis)' },
  { value: 'browser', label: 'ARRISCADO — Playwright logado' }
]

const FALLBACK_ROLE_PRESETS: TaxonomyItem[] = [
  {
    value: 'marketing_growth',
    label: 'Marketing / Growth',
    aliases: ['marketing', 'growth', 'cmo', 'head of marketing', 'demand generation']
  },
  {
    value: 'sales_commercial',
    label: 'Vendas / Comercial',
    aliases: ['sales', 'vendas', 'comercial', 'account executive', 'head of sales']
  },
  {
    value: 'hr_people_talent',
    label: 'RH / People / Talent',
    aliases: ['rh', 'people', 'talent', 'recruiter', 'human resources']
  },
  { value: 'product', label: 'Produto', aliases: ['product', 'produto', 'product manager'] },
  {
    value: 'technology',
    label: 'Tecnologia',
    aliases: ['engineering', 'software engineer', 'tech lead', 'cto']
  },
  { value: 'c_level', label: 'C-Level', aliases: ['ceo', 'founder', 'cfo', 'coo', 'cmo', 'cto'] }
]

const FALLBACK_SENIORITY: TaxonomyItem[] = [
  { value: 'c_level', label: 'C-level / Founder' },
  { value: 'vp', label: 'VP' },
  { value: 'director', label: 'Director' },
  { value: 'head', label: 'Head of' },
  { value: 'manager', label: 'Manager' },
  { value: 'senior', label: 'Senior' }
]

type SearchDepth = NonNullable<SearchRequest['search_depth']>

const SEARCH_DEPTH_OPTIONS: { value: SearchDepth; label: string; hint: string }[] = [
  { value: 'standard', label: 'Rápida', hint: 'menos consultas' },
  { value: 'deep', label: 'Profunda', hint: 'mais variações' }
]

const MODE_HINTS: Record<ScrapeMode, string> = {
  api: 'APIs externas + busca pública. Recomendado.',
  serp: 'Apenas busca pública via SearxNG / Serper.',
  cookie: 'LinkedIn com cookie li_at do seu Keychain.',
  people_search:
    'Busca via /company/<empresa>/people com li_at. Lê os cards do listing direto, sem abrir perfis.',
  browser: 'Playwright logado. Arriscado — só sob demanda.'
}

const ALLOWED_MODES: ScrapeMode[] = ['people_search', 'api']

export default function SearchForm(props: Props) {
  const sourceModes = props.scrapeModes.length ? props.scrapeModes : FALLBACK_MODES
  const modes = sourceModes.filter((m) => ALLOWED_MODES.includes(m.value as ScrapeMode))
  const rolePresets = props.rolePresets.length ? props.rolePresets : FALLBACK_ROLE_PRESETS
  const seniority = props.seniority.length ? props.seniority : FALLBACK_SENIORITY
  const isPeopleSearch = props.form.scrapeMode === 'people_search'

  const toggleSeniority = (value: Seniority) => {
    const next = props.form.filters.seniority.includes(value)
      ? props.form.filters.seniority.filter((item) => item !== value)
      : [...props.form.filters.seniority, value]
    props.onFilterChange({ seniority: next })
  }

  return (
    <form className="card" onSubmit={props.onSubmit}>
      <div className="card-head">
        <h3>Parâmetros da busca</h3>
        <span className="muted">Ctrl + ↵</span>
      </div>
      <div className="card-body">
        <div className="form-row">
          <label className="form-label" htmlFor="company">
            Empresa
          </label>
          <input
            id="company"
            name="companyName"
            className={`input ${props.errors.companyName ? 'invalid' : ''}`}
            aria-invalid={props.errors.companyName ? 'true' : undefined}
            value={props.form.companyName}
            onChange={(e) => props.onChange({ companyName: e.target.value })}
            placeholder="Nubank"
          />
          {props.errors.companyName && <p className="field-error">{props.errors.companyName}</p>}
        </div>

        <div className="form-row">
          <div className="input-grid">
            <div>
              <label className="form-label" htmlFor="domain">
                Domínio
              </label>
              <input
                id="domain"
                name="companyDomain"
                className="input mono"
                autoComplete="url"
                value={props.form.companyDomain}
                onChange={(e) => props.onChange({ companyDomain: e.target.value })}
                placeholder="nubank.com.br"
              />
            </div>
            <div>
              <label className="form-label" htmlFor="max">
                Máx. leads
              </label>
              <input
                id="max"
                name="maxResults"
                type="number"
                min={1}
                max={500}
                className={`input mono ${props.errors.maxResults ? 'invalid' : ''}`}
                value={props.form.maxResults}
                onChange={(e) =>
                  props.onChange({ maxResults: Number.parseInt(e.target.value, 10) || 0 })
                }
              />
              {props.errors.maxResults && <p className="field-error">{props.errors.maxResults}</p>}
            </div>
          </div>
        </div>

        <div className="form-row">
          <label className="form-label" htmlFor="linkedin">
            URL no LinkedIn (opcional)
          </label>
          <input
            id="linkedin"
            name="linkedinUrl"
            type="url"
            className="input mono"
            autoComplete="url"
            value={props.form.linkedinUrl}
            onChange={(e) => props.onChange({ linkedinUrl: e.target.value })}
            placeholder="https://www.linkedin.com/company/nubank/people/"
          />
          <div className="field-hint">
            Pode colar a URL completa da aba <strong>People</strong> (ex.:{' '}
            <code>/company/mercadolivre-com/people/</code>) — usamos diretamente.
          </div>
        </div>

        <div className="form-row">
          <label className="form-label" htmlFor="role-preset">
            Perfil de cargo
          </label>
          <select
            id="role-preset"
            name="rolePreset"
            className="input"
            value={props.form.rolePreset}
            onChange={(e) => props.onRolePresetChange(e.target.value)}
          >
            {rolePresets.map((preset) => (
              <option key={preset.value} value={preset.value}>
                {preset.label}
              </option>
            ))}
            <option value="custom">Customizado</option>
          </select>
        </div>

        <div className="form-row">
          <label className="form-label" htmlFor="titles">
            Palavras-chave
          </label>
          <textarea
            id="titles"
            name="titles"
            className={`input ${props.errors.titles ? 'invalid' : ''}`}
            aria-invalid={props.errors.titles ? 'true' : undefined}
            rows={2}
            value={props.form.titles}
            onChange={(e) => props.onChange({ titles: e.target.value, rolePreset: 'custom' })}
            placeholder="marketing, growth, cmo, head of marketing"
            disabled={props.form.generalSearch}
          />
          <label
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              marginTop: 6,
              fontSize: 12,
              color: 'var(--ink-2)'
            }}
          >
            <input
              type="checkbox"
              checked={props.form.generalSearch}
              onChange={(event) =>
                props.onChange({ generalSearch: event.target.checked })
              }
            />
            Busca geral (sem keywords — traz todos os funcionários visíveis)
          </label>
          {props.errors.titles && <p className="field-error">{props.errors.titles}</p>}
        </div>

        <div className="form-row">
          <label className="form-label">Senioridade</label>
          <div className="chips">
            {seniority.map((item) => {
              const value = item.value as Seniority
              const active = props.form.filters.seniority.includes(value)
              return (
                <button
                  key={item.value}
                  type="button"
                  className={`chip ${active ? 'on' : ''}`}
                  aria-pressed={active}
                  onClick={() => toggleSeniority(value)}
                >
                  {item.label}
                </button>
              )
            })}
          </div>
        </div>

        <div className="form-row">
          <label className="form-label">Modo de coleta</label>
          <div className="seg" style={{ gridTemplateColumns: `repeat(${modes.length}, 1fr)` }}>
            {modes.map((mode) => {
              const active = props.form.scrapeMode === mode.value
              const risky = isRiskyScrapeMode(mode.value as ScrapeMode)
              return (
                <button
                  key={mode.value}
                  type="button"
                  className={`seg-item ${active ? 'on' : ''} ${risky ? 'danger' : ''}`}
                  aria-pressed={active}
                  onClick={() => props.onScrapeModeChange(mode.value as ScrapeMode)}
                >
                  {mode.value.toUpperCase()}
                </button>
              )
            })}
          </div>
          <div className="field-hint">{MODE_HINTS[props.form.scrapeMode]}</div>
          {props.errors.acceptRisk && <p className="field-error">{props.errors.acceptRisk}</p>}
        </div>

        {!isPeopleSearch && (
          <div className="form-row">
            <label className="form-label">Intensidade da busca</label>
            <div className="seg" style={{ gridTemplateColumns: 'repeat(2, 1fr)' }}>
              {SEARCH_DEPTH_OPTIONS.map((option) => {
                const active = props.form.searchDepth === option.value
                return (
                  <button
                    key={option.value}
                    type="button"
                    className={`seg-item ${active ? 'on' : ''}`}
                    aria-pressed={active}
                    onClick={() => props.onChange({ searchDepth: option.value })}
                  >
                    <span style={{ display: 'block', fontWeight: 600 }}>{option.label}</span>
                    <span style={{ display: 'block', fontSize: 10, color: 'var(--ink-3)' }}>
                      {option.hint}
                    </span>
                  </button>
                )
              })}
            </div>
          </div>
        )}

        <div className="form-row">
          <label className="form-label" htmlFor="output">
            Arquivo de saída
          </label>
          <input
            id="output"
            name="outputPath"
            className={`input mono ${props.errors.outputPath ? 'invalid' : ''}`}
            aria-invalid={props.errors.outputPath ? 'true' : undefined}
            value={props.form.outputPath}
            onChange={(e) => props.onChange({ outputPath: e.target.value })}
            placeholder="output/leads.csv"
          />
          {props.errors.outputPath && <p className="field-error">{props.errors.outputPath}</p>}
        </div>

        <div className="form-row">
          <label className="checkbox">
            <input
              type="checkbox"
              checked={props.form.includeUncertain}
              onChange={(e) => props.onChange({ includeUncertain: e.target.checked })}
            />
            Incluir leads sem match claro de cargo
          </label>
          {props.form.includeUncertain && (
            <p className="field-warning" style={{ marginTop: 4, fontSize: 11, color: 'var(--ink-3)' }}>
              ⚠ Com essa opção marcada o filtro de cargo é relaxado — você pode
              receber engineers em uma busca de "marketing". Desmarque para o
              modo estrito (validador por palavra-chave + aliases).
            </p>
          )}
        </div>

        <button type="submit" className="btn-primary" disabled={props.running}>
          {props.running ? (
            <>
              <span className="spinner" /> Buscando…
            </>
          ) : (
            <>
              Buscar leads <span style={{ opacity: 0.7, fontSize: 11 }}>Ctrl↵</span>
            </>
          )}
        </button>
      </div>
    </form>
  )
}
