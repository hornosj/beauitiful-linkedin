import { useEffect, useState } from 'react'
import type {
  ApiKeyOverrides,
  DiagnosticsResponse,
  ProviderDiagnostic
} from '../../../shared/types'
import { ApiClient } from '../../../shared/api'

interface Props {
  client: ApiClient | null
  apiKeys: ApiKeyOverrides
  searchDiagnostics: ProviderDiagnostic[]
  onClose(): void
}

const SOURCE_LABEL: Record<string, string> = {
  env: '.env',
  ui_override: 'UI',
  missing: '—'
}

const SOURCE_TONE: Record<string, string> = {
  env: 'src-env',
  ui_override: 'src-ui',
  missing: 'src-missing'
}

export default function DiagnosticPanel({ client, apiKeys, searchDiagnostics, onClose }: Props) {
  const [config, setConfig] = useState<DiagnosticsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!client) return
    let cancelled = false
    setLoading(true)
    setError(null)
    client
      .diagnostics(apiKeys)
      .then((response) => {
        if (!cancelled) setConfig(response)
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err))
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [client, apiKeys])

  return (
    <div
      className="sheet-overlay"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div className="sheet">
        <aside className="sheet-side">
          <div className="sheet-head-row">
            <h2>Diagnóstico</h2>
            <button type="button" className="sheet-close" onClick={onClose}>
              ✕
            </button>
          </div>
          <div className="dek" style={{ padding: '0 12px' }}>
            Por que sua busca retornou o que retornou. Sem mascarar o problema.
          </div>
        </aside>

        <div className="sheet-body">
          <h3>Última busca — por provider</h3>
          <p className="dek">
            Cada provider relata: registros brutos, leads aceitos, registros descartados pelo filtro
            de empresa/cargo, e qualquer erro HTTP.
          </p>
          {searchDiagnostics.length === 0 ? (
            <div className="sheet-section">
              <p style={{ margin: 0, fontSize: 13, color: 'var(--ink-3)' }}>
                Ainda não houve busca nesta sessão. Rode uma busca para popular este painel.
              </p>
            </div>
          ) : (
            <div className="sheet-section">
              {searchDiagnostics.map((d, idx) => (
                <DiagnosticRow key={`${d.provider}-${idx}`} d={d} />
              ))}
            </div>
          )}

          <h3 style={{ marginTop: 18 }}>Chaves carregadas no sidecar</h3>
          <p className="dek">
            <strong>.env</strong> = veio do arquivo <code>.env</code> na raiz do projeto.{' '}
            <strong>UI</strong> = veio do que você digitou em Preferências (sobrescreve o .env).
            Chaves em branco caem para fallback público.
          </p>
          {loading && (
            <div className="sheet-section">
              <p style={{ margin: 0, fontSize: 13 }}>Carregando…</p>
            </div>
          )}
          {error && (
            <div className="sheet-section">
              <p style={{ margin: 0, color: 'var(--danger, #ff3b30)', fontSize: 13 }}>{error}</p>
            </div>
          )}
          {config && (
            <>
              <div className="sheet-section">
                {config.settings.map((row) => (
                  <div className="row" key={row.field}>
                    <div className="label">
                      <div className="t">{row.env_var}</div>
                      <div className="s" style={{ fontFamily: 'var(--mono)', fontSize: 10 }}>
                        {row.field}
                      </div>
                    </div>
                    <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                      {row.preview && (
                        <code
                          style={{
                            fontFamily: 'var(--mono)',
                            fontSize: 11,
                            color: 'var(--ink-3)',
                            background: 'var(--surface-3)',
                            padding: '2px 6px',
                            borderRadius: 4
                          }}
                        >
                          {row.preview}
                        </code>
                      )}
                      <span className={`api-status ${row.configured ? '' : 'off'}`}>
                        <span className="d" />
                        <span className={`src-tag ${SOURCE_TONE[row.source] ?? ''}`}>
                          {SOURCE_LABEL[row.source] ?? row.source}
                        </span>
                      </span>
                    </div>
                  </div>
                ))}
              </div>

              <h3 style={{ marginTop: 18 }}>Lead providers</h3>
              <div className="sheet-section">
                {config.lead_providers.map((p) => (
                  <div key={p.name} className="row">
                    <div className="label">
                      <div className="t">{p.name}</div>
                      <div className="s">requer: {p.requires.join(', ') || '—'}</div>
                    </div>
                    <span className={`api-status ${p.configured ? '' : 'off'}`}>
                      <span className="d" /> {p.configured ? 'ativo' : 'desligado'}
                    </span>
                  </div>
                ))}
              </div>

              <h3 style={{ marginTop: 18 }}>Search engines</h3>
              <div className="sheet-section">
                {config.search_engines.map((e) => (
                  <div key={e.name} className="row">
                    <div className="label">
                      <div className="t">{e.name}</div>
                      <div className="s">requer: {e.requires.join(', ') || '—'}</div>
                    </div>
                    <span className={`api-status ${e.configured ? '' : 'off'}`}>
                      <span className="d" /> {e.configured ? 'ativo' : 'desligado'}
                    </span>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

function DiagnosticRow({ d }: { d: ProviderDiagnostic }) {
  const total =
    d.raw_records || d.leads_returned || d.dropped_company_evidence || d.dropped_title_filter
  const verdict = verdictFor(d)
  return (
    <div
      style={{
        padding: '10px 12px',
        borderBottom: '1px solid var(--surface-3)',
        display: 'flex',
        flexDirection: 'column',
        gap: 6
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div style={{ display: 'flex', gap: 10, alignItems: 'baseline' }}>
          <strong style={{ fontSize: 13 }}>{d.provider}</strong>
          {d.company_name && (
            <span style={{ fontSize: 11, color: 'var(--ink-3)' }}>· {d.company_name}</span>
          )}
        </div>
        <span
          className={`api-status ${
            verdict.tone === 'ok' ? '' : verdict.tone === 'warn' ? 'warn' : 'off'
          }`}
        >
          <span className="d" /> {verdict.label}
        </span>
      </div>
      <div style={{ fontSize: 11, color: 'var(--ink-2)', display: 'flex', gap: 12, flexWrap: 'wrap' }}>
        <span>raw: <strong>{d.raw_records}</strong></span>
        <span>leads: <strong>{d.leads_returned}</strong></span>
        {d.dropped_company_evidence > 0 && (
          <span>
            dropados por empresa: <strong>{d.dropped_company_evidence}</strong>
          </span>
        )}
        {d.dropped_title_filter > 0 && (
          <span>
            dropados por cargo: <strong>{d.dropped_title_filter}</strong>
          </span>
        )}
        {total === 0 && !d.last_error && <span>nenhum dado retornado</span>}
      </div>
      {d.last_error && (
        <div
          style={{
            fontSize: 11,
            fontFamily: 'var(--mono)',
            color: 'var(--danger, #ff3b30)',
            background: 'var(--surface-3)',
            padding: '6px 8px',
            borderRadius: 4,
            wordBreak: 'break-word'
          }}
        >
          erro: {d.last_error}
        </div>
      )}
      {d.notes.length > 0 && (
        <ul style={{ margin: 0, padding: '0 0 0 16px', fontSize: 11, color: 'var(--ink-3)' }}>
          {d.notes.map((note, i) => (
            <li key={i}>{note}</li>
          ))}
        </ul>
      )}
    </div>
  )
}

function verdictFor(d: ProviderDiagnostic): { tone: 'ok' | 'warn' | 'fail'; label: string } {
  if (d.last_error) return { tone: 'fail', label: 'erro' }
  if (d.leads_returned > 0) return { tone: 'ok', label: 'gerou leads' }
  if (d.raw_records > 0 && d.leads_returned === 0)
    return { tone: 'warn', label: 'tudo dropado pelo filtro' }
  if (d.raw_records === 0) return { tone: 'warn', label: '0 registros' }
  return { tone: 'warn', label: 'sem leads' }
}
