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
    <div className="card min-w-0" style={cardStyle}>
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
          aria-label="Filtrar leads"
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

      <div className="overflow-auto" style={{ maxHeight: 'calc(100vh - 300px)' }}>
        {showSkeleton ? (
          <div className="p-4 space-y-3">
            {Array.from({ length: 7 }).map((_, i) => (
              <div
                key={i}
                className="grid grid-cols-[1.2fr_1.4fr_1fr_0.7fr_0.6fr] gap-4 p-3 border-b border-line animate-pulse"
              >
                {Array.from({ length: 5 }).map((__, j) => (
                  <div key={j} className="h-2 bg-surface-3 rounded-full" />
                ))}
              </div>
            ))}
          </div>
        ) : empty ? (
          <div className="py-16 px-6 flex flex-col items-center justify-center text-center gap-3">
            <div className="w-12 h-12 rounded-full bg-accent/10 text-accent flex items-center justify-center">
              <svg
                width="24"
                height="24"
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
              <div className="text-ink font-semibold text-[15px] mb-1">
                Sem resultados ainda
              </div>
              <div className="text-ink-3 text-[13px] max-w-xs">
                Configure a empresa, escolha o perfil de cargo e rode a busca. Os leads aparecem aqui
                depois da coleta e deduplicação.
              </div>
            </div>
          </div>
        ) : (
          <table className="w-full min-w-[760px] text-left border-collapse text-[13px]">
            <thead className="sticky top-0 z-10 bg-surface/80 backdrop-blur-md">
              <tr>
                <th className="px-4 py-3 font-medium text-ink-3 text-[11px] uppercase tracking-wider border-b border-line w-[34%]">Pessoa</th>
                <th className="px-4 py-3 font-medium text-ink-3 text-[11px] uppercase tracking-wider border-b border-line">Cargo</th>
                <th className="px-4 py-3 font-medium text-ink-3 text-[11px] uppercase tracking-wider border-b border-line w-[110px]">Empresa</th>
                <th className="px-4 py-3 font-medium text-ink-3 text-[11px] uppercase tracking-wider border-b border-line w-[130px]">Fonte</th>
                <th className="px-4 py-3 font-medium text-ink-3 text-[11px] uppercase tracking-wider border-b border-line w-[160px] text-right">Score</th>
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
                    className="group border-b border-line hover:bg-surface-2 transition-colors animate-fade-in-up"
                    style={{ animationDelay: `${Math.min(index, 8) * 35}ms`, animationFillMode: 'both' }}
                  >
                    <td className="px-4 py-3 align-middle">
                      <div className="flex items-center gap-3">
                        <div
                          className="w-8 h-8 rounded-full flex items-center justify-center text-white text-[11px] font-semibold flex-shrink-0"
                          style={{ background: colorFor(seed) }}
                        >
                          {initials(name)}
                        </div>
                        <div className="min-w-0">
                          <div className="font-medium text-ink truncate leading-tight">{name}</div>
                          {lead.linkedin_url ? (
                            <a
                              href={lead.linkedin_url}
                              target="_blank"
                              rel="noreferrer"
                              className="text-[11px] text-ink-3 hover:text-accent truncate block"
                            >
                              {lead.linkedin_url.replace(/^https?:\/\/(www\.)?/, '')}
                            </a>
                          ) : (
                            <div className="text-[11px] text-ink-3 truncate">{lead.source_url}</div>
                          )}
                          {(lead.validation_status === 'maybe_incorrect' || lead.consultation_note) && (
                            <div className="flex flex-wrap gap-1 mt-1.5">
                              {lead.validation_status === 'maybe_incorrect' && (
                                <span
                                  className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-semibold text-amber-700 bg-amber-50 border border-amber-200 dark:bg-amber-900/30 dark:border-amber-800/50 dark:text-amber-300"
                                  title={lead.validation_note ?? undefined}
                                >
                                  Talvez incorreto
                                </span>
                              )}
                              {lead.consultation_note && (
                                <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-semibold text-ink-2 bg-surface-3 border border-line-2">
                                  {lead.consultation_note}
                                </span>
                              )}
                            </div>
                          )}
                        </div>
                      </div>
                    </td>
                    <td className="px-4 py-3 align-middle text-ink-2 truncate max-w-[200px]">{lead.title ?? '—'}</td>
                    <td className="px-4 py-3 align-middle text-ink-2 truncate max-w-[110px]">{lead.company_name}</td>
                    <td className="px-4 py-3 align-middle">
                      <span className="inline-flex items-center gap-1.5 px-2 py-0.5 bg-surface-3 rounded text-[10px] font-mono text-ink-2">
                        <span className="w-1.5 h-1.5 rounded-full bg-ink-4"></span>
                        {lead.source_type}
                      </span>
                    </td>
                    <td className="px-4 py-3 align-middle text-right">
                      <div className="flex items-center justify-end gap-2">
                        <div className="w-12 h-1.5 bg-surface-3 rounded-full overflow-hidden flex-shrink-0">
                          <div
                            className="h-full rounded-full"
                            style={{
                              width: `${Math.max(0, Math.min(100, score))}%`,
                              background: scoreColor(score)
                            }}
                          />
                        </div>
                        <span className="text-[12px] font-semibold text-ink w-6 text-right font-mono">{score}</span>
                      </div>
                      <div className="mt-1.5 flex items-center justify-end gap-1.5">
                        <button
                          type="button"
                          className="px-2.5 py-1.5 bg-surface border border-line shadow-sm rounded text-[11px] font-semibold text-ink-2 hover:bg-surface-3 transition-colors"
                          onClick={() => {
                             if(lead.linkedin_url) window.open(lead.linkedin_url, '_blank')
                          }}
                          disabled={!lead.linkedin_url}
                        >
                          Visitar
                        </button>
                        <button
                          type="button"
                          className="px-2.5 py-1.5 bg-surface border border-line shadow-sm rounded text-[11px] font-semibold text-ink-2 hover:bg-surface-3 transition-colors"
                          onClick={() => {
                             navigator.clipboard.writeText(name)
                          }}
                        >
                          Copiar
                        </button>
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
