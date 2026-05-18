import type { PeopleSearchProbeResponse } from '../../../shared/types'

interface Props {
  probe: PeopleSearchProbeResponse
  onGeneralSearch(): void
  onContinueWithKeywords(): void
  onCancel(): void
}

export default function SmallCompanyDialog(props: Props) {
  const { probe } = props
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Empresa pequena detectada"
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(0,0,0,0.42)',
        display: 'grid',
        placeItems: 'center',
        zIndex: 50
      }}
    >
      <div
        style={{
          background: 'var(--surface, #fff)',
          padding: 22,
          borderRadius: 14,
          maxWidth: 480,
          width: '90%',
          boxShadow: '0 12px 48px rgba(0,0,0,0.18)'
        }}
      >
        <h3 style={{ margin: 0, fontSize: 17 }}>Empresa pequena detectada</h3>
        <p style={{ fontSize: 13, lineHeight: 1.45, color: 'var(--ink-2)' }}>
          Empresas pequenas costumam ter cargos menos padronizados no LinkedIn.
          Deseja fazer uma busca geral por funcionários e filtrar os cargos dentro
          do aplicativo, ou continuar buscando apenas pelas keywords informadas?
        </p>
        {probe.employee_count != null && (
          <p style={{ fontSize: 11, color: 'var(--ink-3)', marginTop: -6 }}>
            Estimativa: ~{probe.employee_count} funcionários{' '}
            <em>({describeSource(probe.source)})</em>
          </p>
        )}
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 14 }}>
          <button
            type="button"
            className="pill-btn primary"
            onClick={props.onGeneralSearch}
          >
            Fazer busca geral
          </button>
          <button
            type="button"
            className="pill-btn"
            onClick={props.onContinueWithKeywords}
          >
            Continuar com keywords
          </button>
          <span style={{ flex: 1 }} />
          <button type="button" className="pill-btn" onClick={props.onCancel}>
            Cancelar
          </button>
        </div>
      </div>
    </div>
  )
}

function describeSource(source: string): string {
  if (source === 'exact') return 'contagem exata'
  if (source === 'range') return 'faixa publicada'
  if (source === 'heuristic') return 'estimativa por cards visíveis'
  return 'fonte desconhecida'
}
