import { useEffect, useState } from 'react'
import type { ApiKeyOverrides, CookieDiagnosticResponse } from '../../../shared/types'
import { ApiClient } from '../../../shared/api'

type Tab = 'general' | 'account' | 'api' | 'providers' | 'export' | 'shortcuts' | 'about'
type ThemeMode = 'light' | 'dark'

interface Props {
  apiKeys: ApiKeyOverrides
  onChange(patch: Partial<ApiKeyOverrides>): void
  onClear(): void
  onClose(): void
  theme: ThemeMode
  setTheme(theme: ThemeMode): void
  client: ApiClient | null
}

interface ApiField {
  key: keyof ApiKeyOverrides
  name: string
  env: string
  desc: string
  secret?: boolean
}

const API_FIELDS: ApiField[] = [
  {
    key: 'serper_api_key',
    name: 'Serper',
    env: 'SERPER_API_KEY',
    desc: 'Pesquisa Google via API. Primeira opção quando SearxNG estiver fora.',
    secret: true
  },
  {
    key: 'brave_search_api_key',
    name: 'Brave Search',
    env: 'BRAVE_SEARCH_API_KEY',
    desc: 'Busca alternativa, sem cota agressiva.',
    secret: true
  },
  {
    key: 'google_custom_search_api_key',
    name: 'Google CSE',
    env: 'GOOGLE_CUSTOM_SEARCH_API_KEY',
    desc: 'Custom Search Engine. Precisa do CX abaixo.',
    secret: true
  },
  {
    key: 'google_custom_search_cx',
    name: 'Google CSE CX',
    env: 'GOOGLE_CUSTOM_SEARCH_CX',
    desc: 'Identificador do mecanismo CSE.'
  },
  {
    key: 'searxng_base_url',
    name: 'SearxNG',
    env: 'SEARXNG_BASE_URL',
    desc: 'Self-hosted. Prioridade quando configurado.'
  },
  {
    key: 'people_data_labs_api_key',
    name: 'People Data Labs',
    env: 'PEOPLE_DATA_LABS_API_KEY',
    desc: 'Resolução de company_id e enriquecimento.',
    secret: true
  },
  {
    key: 'apollo_api_key',
    name: 'Apollo.io',
    env: 'APOLLO_API_KEY',
    desc: 'Estratégia by-company antes do search legado.',
    secret: true
  },
  {
    key: 'coresignal_api_key',
    name: 'Coresignal',
    env: 'CORESIGNAL_API_KEY',
    desc: 'Dados de empresa em larga escala.',
    secret: true
  },
  {
    key: 'lusha_api_key',
    name: 'Lusha',
    env: 'LUSHA_API_KEY',
    desc: 'Contatos verificados; respeita opt-out e LGPD.',
    secret: true
  },
  {
    key: 'snovio_client_id',
    name: 'Snov.io Client ID',
    env: 'SNOVIO_CLIENT_ID',
    desc: 'Enriquecimento de e-mail via OAuth. Use junto com o Client Secret abaixo.'
  },
  {
    key: 'snovio_client_secret',
    name: 'Snov.io Client Secret',
    env: 'SNOVIO_CLIENT_SECRET',
    desc: 'Segredo OAuth do Snov.io. Obrigatório junto com o Client ID.',
    secret: true
  },
  {
    key: 'apify_api_key',
    name: 'Apify Actor',
    env: 'APIFY_API_KEY',
    desc: 'Scraper TypeScript em linkedin-api-scrapper/.',
    secret: true
  },
  {
    key: 'linkedin_li_at_cookie',
    name: 'LinkedIn li_at',
    env: 'LINKEDIN_LI_AT_COOKIE',
    desc: 'Cookie usado pelo modo Cookie. Mantenha local.',
    secret: true
  }
]

const TABS: { id: Tab; lbl: string; ico: string }[] = [
  { id: 'general', lbl: 'Geral', ico: '⚙' },
  { id: 'account', lbl: 'Conta', ico: '◯' },
  { id: 'api', lbl: 'Chaves de API', ico: '⎈' },
  { id: 'providers', lbl: 'Provedores', ico: '⌬' },
  { id: 'export', lbl: 'Exportação', ico: '↧' },
  { id: 'shortcuts', lbl: 'Atalhos', ico: '⌘' },
  { id: 'about', lbl: 'Sobre', ico: 'ⓘ' }
]

const BROWSERS = ['auto', 'chrome', 'edge', 'brave', 'firefox']

const SHORTCUTS: [string, string][] = [
  ['Buscar leads', 'Ctrl ↵'],
  ['Nova busca', 'Ctrl N'],
  ['Abrir preferências', 'Ctrl ,'],
  ['Alternar modo escuro', 'Ctrl ⇧ D'],
  ['Filtrar resultados', 'Ctrl F'],
  ['Exportar CSV', 'Ctrl E']
]

export default function SettingsPanel(props: Props) {
  const [tab, setTab] = useState<Tab>('api')
  const [reveal, setReveal] = useState<Record<string, boolean>>({})
  const [cookieDiag, setCookieDiag] = useState<CookieDiagnosticResponse | null>(null)
  const [cookieDiagLoading, setCookieDiagLoading] = useState(false)
  const [cookieDiagError, setCookieDiagError] = useState<string | null>(null)

  const configuredKey = (k: keyof ApiKeyOverrides): boolean => {
    const v = props.apiKeys[k]
    return typeof v === 'string' && v.trim().length > 0
  }

  const runCookieDiagnostic = async () => {
    if (!props.client) {
      setCookieDiagError('Sidecar não está pronto.')
      return
    }
    setCookieDiagLoading(true)
    setCookieDiagError(null)
    try {
      const result = await props.client.diagnoseCookie({
        cookie: props.apiKeys.linkedin_li_at_cookie ?? null,
        browser: props.apiKeys.linkedin_cookie_browser ?? 'auto'
      })
      setCookieDiag(result)
    } catch (err) {
      setCookieDiagError(err instanceof Error ? err.message : String(err))
    } finally {
      setCookieDiagLoading(false)
    }
  }

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') props.onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [props])

  return (
    <div
      className="sheet-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="settings-title"
      onClick={(e) => {
        if (e.target === e.currentTarget) props.onClose()
      }}
    >
      <div className="sheet">
        <aside className="sheet-side">
          <div className="sheet-head-row">
            <h2 id="settings-title">Preferências</h2>
            <button type="button" className="sheet-close" onClick={props.onClose} aria-label="Fechar preferências" autoFocus>
              ✕
            </button>
          </div>
          {TABS.map((t) => (
            <button
              type="button"
              key={t.id}
              className={`nav-item ${tab === t.id ? 'active' : ''}`}
              aria-pressed={tab === t.id}
              onClick={() => setTab(t.id)}
            >
              <span className="ico">{t.ico}</span> {t.lbl}
            </button>
          ))}
        </aside>

        <div className="sheet-body">
          {tab === 'general' && (
            <>
              <h3>Geral</h3>
              <p className="dek">Aparência e comportamento padrão do aplicativo.</p>

              <div className="sheet-section-title">Aparência</div>
              <div className="sheet-section">
                <div className="row">
                  <div className="label">
                    <div className="t">Modo escuro</div>
                    <div className="s">Troca entre claro e escuro.</div>
                  </div>
                  <button
                    type="button"
                    className={`toggle ${props.theme === 'dark' ? 'on' : ''}`}
                    aria-label="Alternar modo escuro"
                    aria-pressed={props.theme === 'dark'}
                    onClick={() => props.setTheme(props.theme === 'dark' ? 'light' : 'dark')}
                  />
                </div>
                <div className="row">
                  <div className="label">
                    <div className="t">Cor de acento</div>
                    <div className="s">Muda a cor primária dos botões e destaques.</div>
                  </div>
                  <div style={{ display: 'flex', gap: 6 }}>
                    {['#007aff', '#5856d6', '#34c759', '#ff9500', '#ff3b30'].map((c, i) => (
                      <button
                        type="button"
                        key={i}
                        aria-label={`Selecionar cor ${c}`}
                        style={{
                          width: 18,
                          height: 18,
                          borderRadius: '50%',
                          background: c,
                          boxShadow:
                            i === 0
                              ? '0 0 0 2px var(--surface), 0 0 0 3.5px var(--accent)'
                              : 'inset 0 0 0 0.5px rgba(0,0,0,0.1)',
                          cursor: 'pointer'
                        }}
                      />
                    ))}
                  </div>
                </div>
              </div>

              <div className="sheet-section-title">Idioma e região</div>
              <div className="sheet-section">
                <div className="row">
                  <div className="label">
                    <div className="t">Idioma da interface</div>
                    <div className="s">Mensagens em pt-BR, identificadores em inglês.</div>
                  </div>
                  <select className="input" style={{ width: 160, height: 28 }}>
                    <option>Português (BR)</option>
                    <option>English (US)</option>
                  </select>
                </div>
              </div>
            </>
          )}

          {tab === 'account' && (
            <>
              <h3>Conta</h3>
              <p className="dek">
                Informações da sua conta local. Credenciais ficam no cofre seguro do sistema.
              </p>

              <div className="sheet-section-title">Modo recomendado: Chrome aberto (CDP)</div>
              <div
                className="sheet-section"
                style={{ background: 'var(--surface-2)', padding: 12, borderRadius: 6 }}
              >
                <p className="dek" style={{ marginBottom: 8 }}>
                  <strong>Não desloga você do LinkedIn.</strong> O app conecta ao Chrome que você
                  já tem aberto via remote debugging. Reusa sua sessão exata, sem injetar cookie
                  em browser separado.
                </p>
                <p className="dek" style={{ marginBottom: 8 }}>
                  <strong>Como ligar:</strong> feche todas as janelas do Chrome e abra com este
                  comando (ou crie um atalho com ele):
                </p>
                <pre
                  style={{
                    fontFamily: 'var(--mono)',
                    fontSize: 11,
                    background: 'var(--surface-3)',
                    padding: 8,
                    borderRadius: 4,
                    overflowX: 'auto',
                    margin: 0
                  }}
                >
                  {String.raw`"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\Users\${'<seu-usuario>'}\AppData\Local\Google\Chrome\User Data"`}
                </pre>
                <p className="dek" style={{ marginTop: 8 }}>
                  Use o botão <strong>Testar agora</strong> abaixo para confirmar que o app
                  achou o Chrome. Quando aparecer <code>cdp_alive: true</code>, manda bala — você
                  pode rodar a busca tranquilo.
                </p>
              </div>

              <div className="sheet-section-title">Fallback: cookie li_at (pode deslogar)</div>
              <div className="sheet-section">
                <div className="api-row">
                  <div className="api-meta">
                    <span className="api-name">Cookie li_at</span>
                    <span className="api-tag">LINKEDIN_LI_AT_COOKIE</span>
                    <span
                      className={`api-status ${configuredKey('linkedin_li_at_cookie') ? '' : 'off'}`}
                    >
                      <span className="d" />{' '}
                      {configuredKey('linkedin_li_at_cookie') ? 'configurado' : 'em branco'}
                    </span>
                  </div>
                  <div className="api-desc">
                    Cole o valor de <code>li_at</code> do navegador (DevTools &gt; Application &gt;
                    Cookies &gt; linkedin.com). Use isso quando a detecção automática falhar.
                  </div>
                  <div className="key-input">
                    <input
                      type={reveal['linkedin_li_at_cookie'] ? 'text' : 'password'}
                      value={props.apiKeys.linkedin_li_at_cookie ?? ''}
                      placeholder="AQED…ou cole li_at=AQED…; junto com outros cookies"
                      onChange={(e) => props.onChange({ linkedin_li_at_cookie: e.target.value })}
                    />
                    <button
                      type="button"
                      onClick={() =>
                        setReveal((r) => ({
                          ...r,
                          linkedin_li_at_cookie: !r.linkedin_li_at_cookie
                        }))
                      }
                    >
                      {reveal['linkedin_li_at_cookie'] ? 'Ocultar' : 'Revelar'}
                    </button>
                  </div>
                </div>
                <div className="row">
                  <div className="label">
                    <div className="t">Navegador do cookie</div>
                    <div className="s">De onde o app deve tentar ler o cookie li_at.</div>
                  </div>
                  <select
                    className="input"
                    style={{ width: 160, height: 28 }}
                    value={props.apiKeys.linkedin_cookie_browser ?? 'auto'}
                    onChange={(e) =>
                      props.onChange({ linkedin_cookie_browser: e.target.value })
                    }
                  >
                    {BROWSERS.map((b) => (
                      <option key={b} value={b}>
                        {b}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="row">
                  <div className="label">
                    <div className="t">Testar detecção</div>
                    <div className="s">
                      Faz uma rodada do resolver (rookiepy &rarr; browser_cookie3) e mostra qual
                      backend funcionou.
                    </div>
                  </div>
                  <button
                    type="button"
                    className="pill-btn"
                    onClick={runCookieDiagnostic}
                    disabled={cookieDiagLoading || !props.client}
                  >
                    {cookieDiagLoading ? 'Testando…' : 'Testar agora'}
                  </button>
                </div>
                {cookieDiagError && (
                  <p className="field-error" style={{ marginTop: 6 }}>
                    {cookieDiagError}
                  </p>
                )}
                {cookieDiag && (
                  <div
                    className="sheet-section"
                    style={{ marginTop: 8, fontSize: 12, lineHeight: 1.5 }}
                  >
                    <div
                      style={{
                        padding: 6,
                        marginBottom: 6,
                        borderRadius: 4,
                        background: cookieDiag.cdp_alive
                          ? 'rgba(52,199,89,0.12)'
                          : 'rgba(255,149,0,0.12)'
                      }}
                    >
                      <strong>
                        {cookieDiag.cdp_alive
                          ? '✓ Chrome com CDP detectado — modo recomendado disponível'
                          : '⚠ Chrome com CDP não detectado'}
                      </strong>
                      <div className="muted" style={{ fontSize: 11 }}>
                        Endpoint:{' '}
                        <code>{cookieDiag.cdp_endpoint ?? 'http://127.0.0.1:9222'}</code>{' '}
                        — {cookieDiag.cdp_alive ? 'alive' : 'unreachable'}
                      </div>
                    </div>
                    <div>
                      <strong>{cookieDiag.found ? '✓ Cookie encontrado' : '✗ Não achado'}</strong>{' '}
                      <span className="muted">— fonte: {cookieDiag.source}</span>
                      {cookieDiag.preview && (
                        <span className="muted"> ({cookieDiag.preview})</span>
                      )}
                    </div>
                    {cookieDiag.default_browser && (
                      <div className="muted">
                        Navegador padrão detectado: <code>{cookieDiag.default_browser}</code>
                      </div>
                    )}
                    <div className="muted">
                      Backends:{' '}
                      <code>rookiepy={String(cookieDiag.rookiepy_available)}</code>{' '}
                      <code>browser_cookie3={String(cookieDiag.browser_cookie3_available)}</code>
                    </div>
                    {cookieDiag.attempts.length > 0 && (
                      <details style={{ marginTop: 6 }}>
                        <summary>Tentativas ({cookieDiag.attempts.length})</summary>
                        <ul style={{ marginTop: 4, paddingLeft: 18 }}>
                          {cookieDiag.attempts.map((a, i) => (
                            <li key={i} style={{ fontFamily: 'var(--mono)', fontSize: 11 }}>
                              {a.backend}/{a.browser} → {a.found ? 'ok' : 'vazio'}
                              {a.error ? ` (${a.error})` : ''}
                            </li>
                          ))}
                        </ul>
                      </details>
                    )}
                    {!cookieDiag.found && cookieDiag.hints.length > 0 && (
                      <ul style={{ marginTop: 8, paddingLeft: 18 }}>
                        {cookieDiag.hints.map((h, i) => (
                          <li key={i}>{h}</li>
                        ))}
                      </ul>
                    )}
                  </div>
                )}
              </div>

              <div className="sheet-section-title">Zona perigosa</div>
              <div className="sheet-section">
                <div className="row">
                  <div className="label">
                    <div className="t">Limpar todas as chaves</div>
                    <div className="s">Remove tokens e cookies salvos nesta máquina.</div>
                  </div>
                  <button type="button" className="pill-btn danger" onClick={props.onClear}>
                    Limpar
                  </button>
                </div>
              </div>
            </>
          )}

          {tab === 'api' && (
            <>
              <h3>Chaves de API</h3>
              <p className="dek">
                Configure as chaves dos provedores que você usa. Tudo é guardado localmente; nada
                vai para o repositório nem aparece em{' '}
                <code
                  style={{
                    fontFamily: 'var(--mono)',
                    background: 'var(--surface-3)',
                    padding: '1px 5px',
                    borderRadius: 4,
                    fontSize: 11
                  }}
                >
                  .env
                </code>
                .
              </p>

              <div className="sheet-section">
                {API_FIELDS.map((p) => {
                  const value = props.apiKeys[p.key] ?? ''
                  const isSecret = p.secret ?? false
                  return (
                    <div className="api-row" key={p.key}>
                      <div className="api-meta">
                        <span className="api-name">{p.name}</span>
                        <span className="api-tag">{p.env}</span>
                        <span className={`api-status ${value ? '' : 'off'}`}>
                          <span className="d" /> {value ? 'configurado' : 'em branco'}
                        </span>
                      </div>
                      <div className="api-desc">{p.desc}</div>
                      <div className="key-input">
                        <input
                          type={isSecret && !reveal[p.key] ? 'password' : 'text'}
                          value={value}
                          placeholder={`Cole sua chave ${p.name}…`}
                          onChange={(e) => props.onChange({ [p.key]: e.target.value })}
                        />
                        {isSecret && (
                          <button
                            type="button"
                            onClick={() => setReveal((r) => ({ ...r, [p.key]: !r[p.key] }))}
                          >
                            {reveal[p.key] ? 'Ocultar' : 'Revelar'}
                          </button>
                        )}
                      </div>
                    </div>
                  )
                })}
              </div>
            </>
          )}

          {tab === 'providers' && (
            <>
              <h3>Provedores e buscadores</h3>
              <p className="dek">
                Status dos provedores baseados nas chaves configuradas. Use a aba{' '}
                <strong>Chaves de API</strong> para ativar.
              </p>

              <div className="sheet-section-title">Lead providers</div>
              <div className="sheet-section">
                {(
                  [
                    ['people_data_labs_api_key', 'People Data Labs'],
                    ['apollo_api_key', 'Apollo.io'],
                    ['coresignal_api_key', 'Coresignal'],
                    ['lusha_api_key', 'Lusha'],
                    ['apify_api_key', 'Apify LinkedIn Actor']
                  ] as [keyof ApiKeyOverrides, string][]
                ).map(([k, n]) => (
                  <div key={k} className="row">
                    <div className="label">
                      <div className="t">{n}</div>
                      <div
                        className="s"
                        style={{ fontFamily: 'var(--mono)', fontSize: 10 }}
                      >
                        {k}
                      </div>
                    </div>
                    <span className={`api-status ${configuredKey(k) ? '' : 'off'}`}>
                      <span className="d" /> {configuredKey(k) ? 'ativo' : 'desligado'}
                    </span>
                  </div>
                ))}
              </div>

              <div className="sheet-section-title">Search engines</div>
              <div className="sheet-section">
                {(
                  [
                    ['searxng_base_url', 'SearxNG (local)', 'Prioridade quando configurado'],
                    ['serper_api_key', 'Serper', 'Google via API'],
                    ['brave_search_api_key', 'Brave Search', 'Alternativa sem login'],
                    ['google_custom_search_api_key', 'Google CSE', 'Precisa de CX']
                  ] as [keyof ApiKeyOverrides, string, string][]
                ).map(([k, n, d]) => (
                  <div key={k} className="row">
                    <div className="label">
                      <div className="t">{n}</div>
                      <div className="s">{d}</div>
                    </div>
                    <span className={`api-status ${configuredKey(k) ? '' : 'off'}`}>
                      <span className="d" /> {configuredKey(k) ? 'ativo' : 'desligado'}
                    </span>
                  </div>
                ))}
              </div>
            </>
          )}

          {tab === 'export' && (
            <>
              <h3>Exportação</h3>
              <p className="dek">Como o produto final é entregue.</p>
              <div className="sheet-section">
                <div className="row">
                  <div className="label">
                    <div className="t">Formato padrão</div>
                    <div className="s">CSV ou XLSX.</div>
                  </div>
                  <select className="input" style={{ width: 120, height: 28 }}>
                    <option>CSV</option>
                    <option>XLSX</option>
                  </select>
                </div>
                <div className="row">
                  <div className="label">
                    <div className="t">Encoding</div>
                    <div className="s">UTF-8 com BOM para Excel.</div>
                  </div>
                  <button type="button" className="toggle on" aria-label="UTF-8 com BOM ativado" aria-pressed="true" />
                </div>
                <div className="row">
                  <div className="label">
                    <div className="t">Abrir após exportar</div>
                  </div>
                  <button type="button" className="toggle on" aria-label="Abrir após exportar ativado" aria-pressed="true" />
                </div>
              </div>
            </>
          )}

          {tab === 'shortcuts' && (
            <>
              <h3>Atalhos</h3>
              <p className="dek">Atalhos globais do app.</p>
              <div className="sheet-section">
                {SHORTCUTS.map(([n, k]) => (
                  <div className="row" key={n}>
                    <div className="label">
                      <div className="t">{n}</div>
                    </div>
                    <kbd
                      style={{
                        fontFamily: 'var(--mono)',
                        fontSize: 11,
                        background: 'var(--surface-3)',
                        color: 'var(--ink-2)',
                        padding: '3px 7px',
                        borderRadius: 5,
                        boxShadow: 'inset 0 -1px 0 rgba(0,0,0,0.06)'
                      }}
                    >
                      {k}
                    </kbd>
                  </div>
                ))}
              </div>
            </>
          )}

          {tab === 'about' && (
            <>
              <h3>Sobre</h3>
              <p className="dek" style={{ maxWidth: 540 }}>
                Beautiful LinkedIn é uma CLI Python com interface desktop, escrita para descobrir
                leads com postura de pesquisador. Licença MIT.
              </p>
              <div className="sheet-section">
                <div className="row">
                  <div className="label">
                    <div className="t">Versão</div>
                  </div>
                  <div style={{ fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--ink-3)' }}>
                    0.1.8
                  </div>
                </div>
                <div className="row">
                  <div className="label">
                    <div className="t">Verificar atualizações</div>
                  </div>
                  <button type="button" className="pill-btn">
                    Verificar agora
                  </button>
                </div>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
