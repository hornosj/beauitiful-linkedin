import { useEffect, useState } from 'react'
import type {
  ApiKeyOverrides,
  DiagnosticsResponse,
  PlaywrightDiagnosticResponse,
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
  const [pwResult, setPwResult] = useState<PlaywrightDiagnosticResponse | null>(null)
  const [pwLoading, setPwLoading] = useState(false)
  const [pwError, setPwError] = useState<string | null>(null)
  const [cleaning, setCleaning] = useState(false)
  const [cleanError, setCleanError] = useState<string | null>(null)

  const runCleanRun = async (): Promise<void> => {
    const confirmed = window.confirm(
      'Execução limpa\n\n' +
        'Isto vai encerrar processos travados do Beautiful LinkedIn (browser embutido, ' +
        'Chrome de scraping e backend) que estejam disputando as portas de debug e ' +
        'reiniciar o app do zero.\n\n' +
        'Use isto quando a busca parar de funcionar / travar em 0 leads. Continuar?'
    )
    if (!confirmed) return
    setCleaning(true)
    setCleanError(null)
    try {
      await window.beautifulLinkedIn?.cleanRun()
      // O app reinicia logo em seguida; mantemos o estado "limpando" até lá.
    } catch (err) {
      setCleanError(err instanceof Error ? err.message : String(err))
      setCleaning(false)
    }
  }

  const runPlaywrightDiagnostic = async (): Promise<void> => {
    if (!client) return
    setPwLoading(true)
    setPwError(null)
    setPwResult(null)
    try {
      const endpoint = await window.beautifulLinkedIn?.embeddedBrowser
        ?.getCdpEndpoint()
        .catch(() => null)
      const payload = endpoint?.endpoint ? { cdp_endpoint: endpoint.endpoint } : undefined
      const response = await client.diagnosePlaywright(payload)
      setPwResult(response)
    } catch (err) {
      setPwError(err instanceof Error ? err.message : String(err))
    } finally {
      setPwLoading(false)
    }
  }

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

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  return (
    <div
      className="sheet-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="diagnostic-title"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div className="sheet">
        <aside className="sheet-side">
          <div className="sheet-head-row">
            <h2 id="diagnostic-title">Diagnóstico</h2>
            <button type="button" className="sheet-close" onClick={onClose} aria-label="Fechar diagnóstico" autoFocus>
              ✕
            </button>
          </div>
          <div className="dek" style={{ padding: '0 12px' }}>
            Por que sua busca retornou o que retornou. Sem mascarar o problema.
          </div>
        </aside>

        <div className="sheet-body">
          <h3>Playwright / Chromium embutido</h3>
          <p className="dek">
            Auto-teste cronometrado do backend de busca. Mostra exatamente onde travou: probe do CDP,
            import do Playwright, start do driver, conexão e listagem de páginas.
          </p>
          <div className="sheet-section" style={{ padding: 12 }}>
            <button
              type="button"
              className="pill-btn primary"
              onClick={() => void runPlaywrightDiagnostic()}
              disabled={!client || pwLoading}
            >
              {pwLoading ? 'Testando…' : 'Diagnosticar Playwright'}
            </button>
            {pwError && (
              <p style={{ marginTop: 8, color: 'var(--danger, #ff3b30)', fontSize: 12 }}>{pwError}</p>
            )}
            {pwResult && (
              <div style={{ marginTop: 12, display: 'flex', flexDirection: 'column', gap: 8 }}>
                <div style={{ fontSize: 12, color: 'var(--ink-2)' }}>
                  <span>
                    sidecar: <strong>v{pwResult.sidecar_version}</strong>
                  </span>
                  {pwResult.playwright_version && (
                    <span style={{ marginLeft: 12 }}>
                      playwright: <strong>{pwResult.playwright_version}</strong>
                    </span>
                  )}
                  <span style={{ marginLeft: 12 }}>
                    CDP: <code>{pwResult.cdp_endpoint}</code>
                  </span>
                  <span
                    className={`api-status ${pwResult.overall_ok ? '' : 'off'}`}
                    style={{ marginLeft: 12 }}
                  >
                    <span className="d" /> {pwResult.overall_ok ? 'tudo OK' : 'falhou'}
                  </span>
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                  {pwResult.steps.map((step) => (
                    <div
                      key={step.name}
                      style={{
                        display: 'flex',
                        gap: 10,
                        alignItems: 'baseline',
                        padding: '6px 8px',
                        background: step.ok ? 'var(--surface-3)' : 'rgba(255,59,48,0.12)',
                        borderRadius: 4,
                        fontSize: 12
                      }}
                    >
                      <span style={{ minWidth: 130, fontFamily: 'var(--mono)' }}>{step.name}</span>
                      <span
                        style={{
                          color: step.ok ? 'var(--success, #34c759)' : 'var(--danger, #ff3b30)',
                          fontWeight: 600
                        }}
                      >
                        {step.ok ? 'OK' : 'falhou'}
                      </span>
                      <span style={{ color: 'var(--ink-3)' }}>
                        {step.elapsed_ms} ms
                      </span>
                      {step.detail && (
                        <span style={{ color: 'var(--ink-3)', fontSize: 11 }}>· {step.detail}</span>
                      )}
                    </div>
                  ))}
                </div>
                {pwResult.log_path && (
                  <p style={{ margin: 0, fontSize: 11, color: 'var(--ink-3)' }}>
                    Logs detalhados em: <code>{pwResult.log_path}</code>
                  </p>
                )}
              </div>
            )}
          </div>

          <h3 style={{ marginTop: 18 }}>Execução limpa</h3>
          <p className="dek">
            Se a busca travar em 0 leads ou o diagnóstico acima acusar falha em{' '}
            <code>connect_over_cdp</code>, provavelmente sobrou um processo de uma execução
            anterior segurando a porta de debug (9222/9223) e o cache. Este botão encerra esses
            processos e reinicia o app do zero.
          </p>
          <div className="sheet-section" style={{ padding: 12 }}>
            <button
              type="button"
              className="pill-btn"
              onClick={() => void runCleanRun()}
              disabled={cleaning}
            >
              {cleaning ? 'Limpando e reiniciando…' : 'Limpar processos e reiniciar'}
            </button>
            <p style={{ marginTop: 8, fontSize: 11, color: 'var(--ink-3)' }}>
              O app fecha e abre sozinho. Sua sessão do LinkedIn é preservada.
            </p>
            {cleanError && (
              <p style={{ marginTop: 8, color: 'var(--danger, #ff3b30)', fontSize: 12 }}>
                {cleanError}
              </p>
            )}
          </div>

          <h3 style={{ marginTop: 18 }}>Última busca — por provider</h3>
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
