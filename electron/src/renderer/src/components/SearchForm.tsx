import type { ScrapeMode, SearchRequest, Seniority, TaxonomyItem } from '../../../shared/types'
import type {
  ExtraCompany,
  SearchFormFilters,
  SearchFormState,
  ValidationErrors
} from '../../../shared/validation'

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

export default function SearchForm(props: Props) {
  const rolePresets = props.rolePresets.length ? props.rolePresets : FALLBACK_ROLE_PRESETS
  const seniority = props.seniority.length ? props.seniority : FALLBACK_SENIORITY
  const isPeopleSearch = props.form.scrapeMode === 'people_search'

  const extraCompanies = props.form.extraCompanies
  const companyCount =
    (props.form.companyName.trim() ? 1 : 0) +
    extraCompanies.filter((entry) => entry.name.trim()).length
  const isMultiCompany = companyCount >= 2

  const setExtraCompanies = (next: ExtraCompany[]) => props.onChange({ extraCompanies: next })
  const addCompany = () =>
    setExtraCompanies([
      ...extraCompanies,
      { name: '', domain: '', linkedinUrl: '', maxResults: props.form.maxResults }
    ])
  const updateCompany = (index: number, patch: Partial<ExtraCompany>) =>
    setExtraCompanies(
      extraCompanies.map((entry, i) => (i === index ? { ...entry, ...patch } : entry))
    )
  const removeCompany = (index: number) =>
    setExtraCompanies(extraCompanies.filter((_, i) => i !== index))

  const toggleSeniority = (value: Seniority) => {
    const next = props.form.filters.seniority.includes(value)
      ? props.form.filters.seniority.filter((item) => item !== value)
      : [...props.form.filters.seniority, value]
    props.onFilterChange({ seniority: next })
  }

  // "A partir de" é açúcar de UI: marca o nível escolhido e todos acima
  // dele (a lista vem ordenada do mais sênior para o menos). Popula os
  // mesmos chips — não muda a semântica do filtro no backend.
  const seniorityValues = seniority.map((item) => item.value as Seniority)
  const fromLevelValue = (() => {
    const active = props.form.filters.seniority
    if (active.length === 0) return ''
    const indices = active.map((value) => seniorityValues.indexOf(value))
    if (indices.some((index) => index < 0)) return ''
    const maxIndex = Math.max(...indices)
    const prefix = seniorityValues.slice(0, maxIndex + 1)
    const isExactPrefix =
      prefix.length === active.length && prefix.every((value) => active.includes(value))
    return isExactPrefix ? seniorityValues[maxIndex] : ''
  })()
  const applyFromLevel = (value: string) => {
    if (!value) {
      props.onFilterChange({ seniority: [] })
      return
    }
    const index = seniorityValues.indexOf(value as Seniority)
    if (index < 0) return
    props.onFilterChange({ seniority: seniorityValues.slice(0, index + 1) })
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
          <label className="form-label">Outras empresas</label>
          {extraCompanies.length > 0 && (
            <label
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                marginBottom: 8,
                fontSize: 13,
                color: 'var(--ink-2)'
              }}
            >
              <input
                type="checkbox"
                checked={props.form.sameMaxForAll}
                onChange={(e) => props.onChange({ sameMaxForAll: e.target.checked })}
              />
              Mesmo máx. de leads para todas as empresas
            </label>
          )}
          {extraCompanies.length > 0 && (
            <div style={{ display: 'grid', gap: 6, marginBottom: 8 }}>
              {extraCompanies.map((entry, index) => (
                <div
                  key={index}
                  style={{
                    display: 'grid',
                    gap: 6,
                    border: '1px solid var(--line, #e5e7eb)',
                    borderRadius: 8,
                    padding: 8
                  }}
                >
                  <div
                    style={{
                      display: 'grid',
                      gridTemplateColumns: props.form.sameMaxForAll
                        ? '1fr 1fr auto'
                        : '1fr 1fr 84px auto',
                      gap: 6
                    }}
                  >
                    <input
                      className="input"
                      value={entry.name}
                      onChange={(e) => updateCompany(index, { name: e.target.value })}
                      placeholder="Nome da empresa"
                      aria-label={`Empresa adicional ${index + 1}`}
                    />
                    <input
                      className="input mono"
                      value={entry.domain}
                      onChange={(e) => updateCompany(index, { domain: e.target.value })}
                      placeholder="dominio.com.br"
                      autoComplete="url"
                      aria-label={`Domínio da empresa adicional ${index + 1}`}
                    />
                    {!props.form.sameMaxForAll && (
                      <input
                        className="input mono"
                        type="number"
                        min={1}
                        max={500}
                        value={entry.maxResults}
                        onChange={(e) =>
                          updateCompany(index, {
                            maxResults: Number.parseInt(e.target.value, 10) || 0
                          })
                        }
                        placeholder="máx"
                        title="Máx. de leads desta empresa"
                        aria-label={`Máx. de leads da empresa adicional ${index + 1}`}
                      />
                    )}
                    <button
                      type="button"
                      className="pill-btn"
                      onClick={() => removeCompany(index)}
                      aria-label="Remover empresa"
                      title="Remover empresa"
                      style={{ flexShrink: 0 }}
                    >
                      ✕
                    </button>
                  </div>
                  <input
                    className="input mono"
                    type="url"
                    value={entry.linkedinUrl}
                    onChange={(e) => updateCompany(index, { linkedinUrl: e.target.value })}
                    placeholder="https://www.linkedin.com/company/empresa/people/"
                    autoComplete="url"
                    aria-label={`URL da aba People da empresa adicional ${index + 1}`}
                  />
                </div>
              ))}
            </div>
          )}
          <button type="button" className="pill-btn" onClick={addCompany}>
            + Adicionar empresa
          </button>
          <div className="field-hint">
            Cada empresa dispara uma busca própria, em sequência. O domínio é
            usado para encontrar e-mails; a URL da aba <strong>People</strong> é o
            que o scraper visita (sem ela, tentamos adivinhar pelo nome).
            Desmarque <em>“mesmo máx.”</em> para definir quantos leads puxar de
            cada empresa. Uma falha não interrompe as demais.
          </div>
        </div>

        {isMultiCompany && (
          <div className="form-row">
            <label className="form-label">Organização dos resultados</label>
            <div className="seg" style={{ gridTemplateColumns: 'repeat(2, 1fr)' }}>
              <button
                type="button"
                className={`seg-item ${props.form.tableMode === 'single' ? 'on' : ''}`}
                aria-pressed={props.form.tableMode === 'single'}
                onClick={() => props.onChange({ tableMode: 'single' })}
              >
                <span style={{ display: 'block', fontWeight: 600 }}>Tabela única</span>
                <span style={{ display: 'block', fontSize: 10, color: 'var(--ink-3)' }}>
                  coluna identifica a empresa
                </span>
              </button>
              <button
                type="button"
                className={`seg-item ${props.form.tableMode === 'separate' ? 'on' : ''}`}
                aria-pressed={props.form.tableMode === 'separate'}
                onClick={() => props.onChange({ tableMode: 'separate' })}
              >
                <span style={{ display: 'block', fontWeight: 600 }}>Tabelas separadas</span>
                <span style={{ display: 'block', fontSize: 10, color: 'var(--ink-3)' }}>
                  uma tabela por empresa
                </span>
              </button>
            </div>
          </div>
        )}

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
          <select
            className="input"
            style={{ marginBottom: 8 }}
            value={fromLevelValue}
            onChange={(e) => applyFromLevel(e.target.value)}
            aria-label="Selecionar senioridade a partir de um nível"
          >
            <option value="">A partir de… (atalho — marca este nível e acima)</option>
            {seniority.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label} e acima
              </option>
            ))}
          </select>
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
              {isMultiCompany ? `Buscar leads · ${companyCount} empresas` : 'Buscar leads'}{' '}
              <span style={{ opacity: 0.7, fontSize: 11 }}>Ctrl↵</span>
            </>
          )}
        </button>
      </div>
    </form>
  )
}
