export type CompanyRunStatus = 'pending' | 'running' | 'done' | 'error'

export interface CompanyRunView {
  id: string
  name: string
  status: CompanyRunStatus
  leadCount: number
  error?: string
}

const STATUS_META: Record<CompanyRunStatus, { label: string; color: string }> = {
  pending: { label: 'Na fila', color: 'var(--ink-4)' },
  running: { label: 'Buscando…', color: 'var(--accent)' },
  done: { label: 'Concluída', color: 'var(--success)' },
  error: { label: 'Erro', color: 'var(--risky)' }
}

/**
 * Compact per-company status list shown during a multi-company search.
 * One row per company with a status dot/spinner, the company name, and
 * either the lead count (done), an isolated error message, or the queue
 * state. Reuses the card / spinner design tokens — no new visual style.
 */
export default function CompanySearchRuns({ runs }: { runs: CompanyRunView[] }) {
  if (runs.length === 0) return null
  const done = runs.filter((run) => run.status === 'done').length
  const errors = runs.filter((run) => run.status === 'error').length

  return (
    <div className="card min-w-0">
      <div className="card-head">
        <h3>
          Empresas
          <span style={{ color: 'var(--ink-3)', fontWeight: 400, fontSize: 13, marginLeft: 6 }}>
            · {done}/{runs.length} concluídas
            {errors ? ` · ${errors} com erro` : ''}
          </span>
        </h3>
      </div>
      <div style={{ display: 'grid', gap: 2, padding: '6px 8px' }}>
        {runs.map((run) => {
          const meta = STATUS_META[run.status]
          const trailing =
            run.status === 'error'
              ? run.error ?? meta.label
              : run.status === 'done'
                ? `${run.leadCount} ${run.leadCount === 1 ? 'lead' : 'leads'}`
                : meta.label
          return (
            <div
              key={run.id}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 10,
                padding: '7px 8px',
                borderRadius: 8
              }}
            >
              <span style={{ width: 16, display: 'grid', placeItems: 'center', flexShrink: 0 }}>
                {run.status === 'running' ? (
                  <span className="spinner" style={{ width: 12, height: 12 }} />
                ) : (
                  <span
                    style={{
                      width: 8,
                      height: 8,
                      borderRadius: 99,
                      background: meta.color
                    }}
                  />
                )}
              </span>
              <span
                style={{
                  fontSize: 13,
                  color: 'var(--ink)',
                  fontWeight: 500,
                  minWidth: 0,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap'
                }}
                title={run.name}
              >
                {run.name}
              </span>
              <span
                style={{
                  marginLeft: 'auto',
                  fontSize: 12,
                  color: run.status === 'error' ? 'var(--risky)' : 'var(--ink-3)',
                  fontVariantNumeric: 'tabular-nums',
                  maxWidth: '55%',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap'
                }}
                title={run.status === 'error' ? run.error : undefined}
              >
                {trailing}
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}
