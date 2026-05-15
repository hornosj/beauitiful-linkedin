import { useMemo, useState } from 'react'
import type { Lead, ProspectingSummary } from '../../../shared/types'

interface Props {
  leads: Lead[]
  total: number
  summary: ProspectingSummary | null
  loading?: boolean
  pendingLeadCount?: number
  query: string
  onQueryChange(value: string): void
  filter: 'all' | 'head' | 'growth'
  onFilterChange(value: 'all' | 'head' | 'growth'): void
  onSaveCurrent?(name: string): void
  saveCurrentDisabled?: boolean
  suggestedSaveName?: string
}

const AVATAR_PALETTE = [
  '#ff9500',
  '#ff3b30',
  '#5856d6',
  '#34c759',
  '#007aff',
  '#ff2d55',
  '#af52de',
  '#5ac8fa'
]

function colorFor(seed: string): string {
  let hash = 0
  for (let i = 0; i < seed.length; i++) hash = (hash * 31 + seed.charCodeAt(i)) | 0
  return AVATAR_PALETTE[Math.abs(hash) % AVATAR_PALETTE.length]
}

function initials(name: string): string {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((p) => p[0]?.toUpperCase() ?? '')
    .join('')
}

function scoreColor(score: number): string {
  if (score >= 95) return '#34c759'
  if (score >= 85) return '#007aff'
  if (score >= 70) return '#ff9500'
  return '#ff3b30'
}

export default function ResultsTable(props: Props) {
  const empty = !props.leads.length && !props.loading
  const showSkeleton = !props.leads.length && !!props.loading
  const [saveDialogOpen, setSaveDialogOpen] = useState(false)
  const [saveName, setSaveName] = useState('')

  const cardStyle = useMemo<React.CSSProperties>(
    () => ({ display: 'flex', flexDirection: 'column' }),
    []
  )

  const openSaveDialog = () => {
    setSaveName(props.suggestedSaveName || 'Leads salvos')
    setSaveDialogOpen(true)
  }

  const submitSave = () => {
    const cleaned = saveName.trim()
    if (!cleaned || !props.onSaveCurrent) return
    props.onSaveCurrent(cleaned)
    setSaveDialogOpen(false)
  }

  return (
    <div className="card" style={cardStyle}>
      <div className="card-head">
        <h3>
          Resultados
          <span style={{ color: 'var(--ink-3)', fontWeight: 400, fontSize: 13, marginLeft: 6 }}>
            · {props.leads.length} leads
          </span>
          {props.pendingLeadCount ? (
            <span style={{ marginLeft: 8, fontSize: 12, color: 'var(--accent)' }}>
              +{props.pendingLeadCount} entrando
            </span>
          ) : null}
        </h3>
        <div style={{ display: 'flex', gap: 6 }}>
          {props.onSaveCurrent && (
            <button
              type="button"
              className="pill-btn primary"
              onClick={openSaveDialog}
              disabled={props.saveCurrentDisabled}
            >
              Salvar busca atual
            </button>
          )}
          <button type="button" className="pill-btn">
            Exportar CSV
          </button>
          <button type="button" className="pill-btn">
            XLSX
          </button>
        </div>
      </div>

      {saveDialogOpen && (
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: '1fr auto auto',
            gap: 8,
            alignItems: 'end',
            padding: '10px 14px',
            borderBottom: '0.5px solid var(--line)',
            background: 'var(--surface-2)'
          }}
        >
          <label style={{ display: 'grid', gap: 4, fontSize: 11, color: 'var(--ink-3)' }}>
            Nome da tabela salva
            <input
              className="search-input"
              value={saveName}
              onChange={(event) => setSaveName(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') submitSave()
                if (event.key === 'Escape') setSaveDialogOpen(false)
              }}
              autoFocus
            />
          </label>
          <button
            type="button"
            className="pill-btn primary"
            onClick={submitSave}
            disabled={!saveName.trim()}
          >
            Salvar
          </button>
          <button type="button" className="pill-btn" onClick={() => setSaveDialogOpen(false)}>
            Cancelar
          </button>
        </div>
      )}

      <div className="table-tools">
        <input
          className="search-input"
          placeholder="Filtrar leads…"
          value={props.query}
          onChange={(e) => props.onQueryChange(e.target.value)}
        />
        <span style={{ flex: 1 }} />
        <div className="seg-mini">
          <button
            className={props.filter === 'all' ? 'on' : ''}
            onClick={() => props.onFilterChange('all')}
          >
            Todos
          </button>
          <button
            className={props.filter === 'head' ? 'on' : ''}
            onClick={() => props.onFilterChange('head')}
          >
            Head
          </button>
          <button
            className={props.filter === 'growth' ? 'on' : ''}
            onClick={() => props.onFilterChange('growth')}
          >
            Growth
          </button>
        </div>
      </div>

      {props.summary && (
        <div
          style={{
            padding: '8px 14px',
            borderBottom: '0.5px solid var(--line)',
            background: 'var(--surface-2)',
            fontSize: 11,
            color: 'var(--ink-3)'
          }}
        >
          brutos: {props.summary.total_raw_leads} · únicos: {props.summary.total_deduplicated_leads}
          {props.summary.total_previously_consulted_leads
            ? ` · já consultados: ${props.summary.total_previously_consulted_leads}`
            : ''}{' '}
          {props.summary.total_maybe_incorrect_leads
            ? `· talvez incorretos: ${props.summary.total_maybe_incorrect_leads} `
            : ''}
          · arquivo: {props.summary.output_file}
        </div>
      )}

      <div style={{ maxHeight: 580, overflow: 'auto' }}>
        {showSkeleton ? (
          <div style={{ padding: 14 }}>
            {Array.from({ length: 7 }).map((_, i) => (
              <div
                key={i}
                style={{
                  display: 'grid',
                  gridTemplateColumns: '1.2fr 1.4fr 1fr 0.7fr 0.6fr',
                  gap: 14,
                  padding: '10px 4px',
                  borderBottom: '0.5px solid var(--line)'
                }}
              >
                {Array.from({ length: 5 }).map((__, j) => (
                  <div key={j} className="loading-line" style={{ height: 8 }} />
                ))}
              </div>
            ))}
          </div>
        ) : empty ? (
          <div
            style={{
              padding: '60px 24px',
              textAlign: 'center',
              color: 'var(--ink-3)',
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              gap: 12
            }}
          >
            <div
              style={{
                width: 44,
                height: 44,
                borderRadius: 99,
                background: 'var(--accent-tint)',
                color: 'var(--accent)',
                display: 'grid',
                placeItems: 'center'
              }}
            >
              <svg
                width="22"
                height="22"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <circle cx="11" cy="11" r="7" />
                <path d="m21 21-4.3-4.3" />
              </svg>
            </div>
            <div>
              <div style={{ color: 'var(--ink)', fontWeight: 600, fontSize: 14, marginBottom: 4 }}>
                Sem resultados ainda
              </div>
              <div style={{ fontSize: 12, maxWidth: 320 }}>
                Configure a empresa, escolha o perfil de cargo e rode a busca. Os leads aparecem aqui
                depois da coleta e deduplicação.
              </div>
            </div>
          </div>
        ) : (
          <table className="leads">
            <thead>
              <tr>
                <th style={{ width: '34%' }}>Pessoa</th>
                <th>Cargo</th>
                <th style={{ width: 110 }}>Empresa</th>
                <th style={{ width: 130 }}>Fonte</th>
                <th style={{ width: 130, textAlign: 'right' }}>Score</th>
              </tr>
            </thead>
            <tbody>
              {props.leads.map((lead, index) => {
                const name = lead.person_name ?? '—'
                const seed = lead.linkedin_url ?? lead.source_url ?? name
                const score = lead.confidence_score
                return (
                  <tr
                    key={lead.linkedin_url ?? lead.source_url ?? index}
                    className="lead-row-enter"
                    style={{ animationDelay: `${Math.min(index, 8) * 35}ms` }}
                  >
                    <td>
                      <div className="person">
                        <div className="avatar" style={{ background: colorFor(seed) }}>
                          {initials(name)}
                        </div>
                        <div>
                          <div className="nm">{name}</div>
                          {lead.linkedin_url ? (
                            <a
                              href={lead.linkedin_url}
                              target="_blank"
                              rel="noreferrer"
                              className="hd"
                              style={{ textDecoration: 'none' }}
                            >
                              {lead.linkedin_url.replace(/^https?:\/\/(www\.)?/, '')}
                            </a>
                          ) : (
                            <div className="hd">{lead.source_url}</div>
                          )}
                          {(lead.validation_status === 'maybe_incorrect' || lead.consultation_note) && (
                            <div className="lead-badges">
                              {lead.validation_status === 'maybe_incorrect' && (
                                <span
                                  className="lead-badge warning"
                                  title={lead.validation_note ?? undefined}
                                >
                                  Talvez incorreto
                                </span>
                              )}
                              {lead.consultation_note && (
                                <span className="lead-badge consulted">
                                  {lead.consultation_note}
                                </span>
                              )}
                            </div>
                          )}
                        </div>
                      </div>
                    </td>
                    <td>{lead.title ?? '—'}</td>
                    <td>{lead.company_name}</td>
                    <td>
                      <span className="src-tag">● {lead.source_type}</span>
                    </td>
                    <td>
                      <div className="score-bar">
                        <div className="bar">
                          <div
                            style={{
                              width: `${Math.max(0, Math.min(100, score))}%`,
                              background: scoreColor(score)
                            }}
                          />
                        </div>
                        <span className="v">{score}</span>
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
