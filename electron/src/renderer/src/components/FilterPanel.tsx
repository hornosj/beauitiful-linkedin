import type { JobFunction, TaxonomyItem } from '../../../shared/types'
import type { SearchFormFilters } from '../../../shared/validation'

interface Props {
  filters: SearchFormFilters
  functions: TaxonomyItem[]
  onChange(patch: Partial<SearchFormFilters>): void
}

export default function FilterPanel(props: Props) {
  const toggleFunction = (value: JobFunction) => {
    const next = props.filters.functions.includes(value)
      ? props.filters.functions.filter((item) => item !== value)
      : [...props.filters.functions, value]
    props.onChange({ functions: next })
  }

  return (
    <section className="card">
      <div className="card-head">
        <h3>Filtros avançados</h3>
        <span className="muted">vazio = manter todos</span>
      </div>
      <div className="card-body">
        <div className="form-row">
          <label className="form-label">Função</label>
          <div className="chips">
            {props.functions.map((item) => {
              const active = props.filters.functions.includes(item.value as JobFunction)
              return (
                <button
                  key={item.value}
                  type="button"
                  className={`chip ${active ? 'on' : ''}`}
                  onClick={() => toggleFunction(item.value as JobFunction)}
                >
                  {item.label}
                </button>
              )
            })}
          </div>
        </div>

        <div className="form-row">
          <div className="input-grid">
            <div>
              <label className="form-label" htmlFor="locations">
                Localização
              </label>
              <input
                id="locations"
                className="input"
                placeholder="brasil, sao paulo"
                value={props.filters.locations}
                onChange={(e) => props.onChange({ locations: e.target.value })}
              />
            </div>
            <div>
              <label className="form-label" htmlFor="exclude">
                Excluir títulos
              </label>
              <input
                id="exclude"
                className="input"
                placeholder="recruiter, headhunter"
                value={props.filters.excludeTitles}
                onChange={(e) => props.onChange({ excludeTitles: e.target.value })}
              />
            </div>
          </div>
        </div>

        <div className="form-row">
          <div className="input-grid">
            <div>
              <label className="form-label" htmlFor="confidence">
                Confiança mínima
              </label>
              <input
                id="confidence"
                type="number"
                min={0}
                max={100}
                className="input mono"
                placeholder="60"
                value={props.filters.minConfidence}
                onChange={(e) => props.onChange({ minConfidence: e.target.value })}
              />
            </div>
            <label className="checkbox" style={{ alignItems: 'flex-end', paddingBottom: 6 }}>
              <input
                type="checkbox"
                checked={props.filters.dropUnclassified}
                onChange={(e) => props.onChange({ dropUnclassified: e.target.checked })}
              />
              Descartar não-classificados
            </label>
          </div>
        </div>
      </div>
    </section>
  )
}
