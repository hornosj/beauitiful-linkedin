import { useEffect, useRef, useState } from 'react'

type Phase =
  | 'checking'
  | 'launching'
  | 'waiting_cdp'
  | 'awaiting_target'
  | 'settling'
  | 'ready'
  | 'failed'
  | 'preparing'
  | 'login_required'

interface Props {
  targetUrl: string
  // Substring used to detect that an open tab landed on the target page.
  targetSlug: string
  targetKind?: 'people' | 'profile'
  /** 'external' = spawn Chrome (default), 'embedded' = use in-app WebContentsView. */
  mode?: 'external' | 'embedded'
  /** li_at cookie to inject when mode='embedded'. */
  liAt?: string
  onReady(): void
  onCancel(): void
  /** Called in embedded mode when li_at is rejected (authwall). Falls back to external Chrome. */
  onFallback?(): void
  settleMs?: number
}

const POLL_MS = 1500
const DEFAULT_SETTLE_MS = 3000
const LOGIN_HINTS = ['/login', '/checkpoint', '/uas/login', '/authwall', '/signup']

export default function ChromeBootstrapModal(props: Props) {
  const targetKind = props.targetKind ?? 'people'
  const mode = props.mode ?? 'external'
  const [phase, setPhase] = useState<Phase>(mode === 'embedded' ? 'preparing' : 'checking')
  const [error, setError] = useState<string | null>(null)
  const [settleProgress, setSettleProgress] = useState(0)
  const [hint, setHint] = useState<'loading' | 'login' | null>(null)
  const cancelledRef = useRef(false)
  // True once we have ensured a tab is pointing at the target URL — used so
  // we don't repeatedly open new tabs while the user is busy logging in.
  const tabEnsuredRef = useRef(false)
  const settleMs = props.settleMs ?? DEFAULT_SETTLE_MS

  useEffect(() => {
    cancelledRef.current = false
    tabEnsuredRef.current = false
    return () => {
      cancelledRef.current = true
    }
  }, [])

  // Embedded mode: inject li_at into the in-app WebContentsView and navigate directly.
  // If the cookie is rejected (authwall), invoke onFallback so the caller can switch
  // to the external Chrome path instead.
  useEffect(() => {
    if (phase !== 'preparing' || mode !== 'embedded') return
    void (async () => {
      console.log('[ChromeBootstrapModal] modo=embedded fase=preparing → iniciando')
      const bridge = window.beautifulLinkedIn?.embeddedBrowser
      if (!bridge) {
        console.error('[ChromeBootstrapModal] embeddedBrowser bridge indisponível em window.beautifulLinkedIn')
        setError('Bridge do browser embutido indisponível.')
        setPhase('failed')
        return
      }
      const liAt = props.liAt?.trim() ?? ''
      if (!liAt) {
        console.error('[ChromeBootstrapModal] li_at vazio — não configurado')
        setError('Cookie li_at não configurado. Configure-o em Conta > Cookie li_at.')
        setPhase('failed')
        return
      }
      console.log(`[ChromeBootstrapModal] Chamando bridge.prepare(liAt="${liAt.slice(0,8)}…", url="${props.targetUrl}")`)
      const result = await bridge.prepare(liAt, props.targetUrl)
      console.log('[ChromeBootstrapModal] bridge.prepare() retornou:', JSON.stringify(result))
      if (cancelledRef.current) {
        console.log('[ChromeBootstrapModal] cancelado após prepare()')
        return
      }
      if (!result.ready) {
        if (result.needsLogin) {
          console.warn('[ChromeBootstrapModal] needsLogin=true — janela de login separada aberta por prepare().')
          // prepare() já abriu uma BrowserWindow dedicada em /login (input confiável no Windows).
          setPhase('login_required')
          return
        }
        console.error('[ChromeBootstrapModal] prepare() não pronto. error=', result.error)
        setError(result.error ?? 'Falha ao carregar a página do LinkedIn.')
        setPhase('failed')
        return
      }
      if (result.onAuthwall) {
        console.warn('[ChromeBootstrapModal] AUTHWALL detectado → abrindo janela de login separada.')
        // Nunca exibimos a WebContentsView para login: ela perde input no Windows.
        // A janela separada (reloadLogin abre uma se não houver) sempre recebe teclado/mouse.
        await bridge.reloadLogin()
        setPhase('login_required')
        return
      }
      console.log('[ChromeBootstrapModal] Página carregada com sucesso ✓ → fase settling')
      setPhase('settling')
    })()
  }, [phase, mode, props.liAt, props.targetUrl, props.onFallback])

  // Phase: checking — probe CDP, then either jump to launching or to target
  // monitoring if CDP is already alive from a previous run.
  useEffect(() => {
    if (phase !== 'checking') return
    void (async () => {
      const bridge = window.beautifulLinkedIn?.chrome
      if (!bridge) {
        setError('Bridge do Electron indisponível.')
        setPhase('failed')
        return
      }
      const { alive } = await bridge.probe()
      if (cancelledRef.current) return
      if (alive) {
        setPhase('awaiting_target')
      } else {
        setPhase('launching')
      }
    })()
  }, [phase])

  // Phase: launching — spawn the dedicated Chrome with the target URL as the
  // initial tab. Chrome will redirect to /login if needed.
  useEffect(() => {
    if (phase !== 'launching') return
    void (async () => {
      const bridge = window.beautifulLinkedIn.chrome
      const result = await bridge.launch(props.targetUrl)
      if (cancelledRef.current) return
      if (!result.launched) {
        setError(result.error ?? 'Não foi possível iniciar o Chrome.')
        setPhase('failed')
        return
      }
      tabEnsuredRef.current = true // launch already opened the URL
      setPhase('waiting_cdp')
    })()
  }, [phase, props.targetUrl])

  useEffect(() => {
    if (phase !== 'waiting_cdp') return
    void (async () => {
      const bridge = window.beautifulLinkedIn.chrome
      const alive = await bridge.waitForCdp(30000)
      if (cancelledRef.current) return
      if (alive) {
        setPhase('awaiting_target')
        return
      }
      await bridge.kill()
      if (cancelledRef.current) return
      const launch = await bridge.launch(props.targetUrl)
      if (cancelledRef.current) return
      if (launch.launched) {
        const aliveAfterRetry = await bridge.waitForCdp(30000)
        if (cancelledRef.current) return
        if (aliveAfterRetry) {
          tabEnsuredRef.current = true
          setPhase('awaiting_target')
          return
        }
      }
      setError(
        'Chrome não expôs a porta de debug 9222. Verifique no Gerenciador de Tarefas se ' +
          'outro processo já está ocupando essa porta.'
      )
      setPhase('failed')
    })()
  }, [phase, props.targetUrl])

  // Phase: awaiting_target — single-shot ensure a tab is on the target URL,
  // then poll until one of the existing tabs reaches the target URL.
  // No new tabs are opened during this phase.
  useEffect(() => {
    if (phase !== 'awaiting_target') return
    let cancelled = false
    const bridge = window.beautifulLinkedIn.chrome

    const ensureTabOnce = async () => {
      if (tabEnsuredRef.current) return
      tabEnsuredRef.current = true
      const tabs = await bridge.listTabs()
      if (cancelled || cancelledRef.current) return
      const slug = props.targetSlug.toLowerCase()
      const alreadyOnTarget = tabs.some((t) => {
        const u = (t.url || '').toLowerCase()
        if (targetKind === 'people') {
          return u.includes(slug) || u.includes('/feed') || u.includes('/login')
        }
        return u.includes(slug) || LOGIN_HINTS.some((hint) => u.includes(hint))
      })
      if (!alreadyOnTarget) {
        await bridge.openUrl(props.targetUrl)
      }
    }

    const tick = async () => {
      if (cancelled || cancelledRef.current) return
      const tabs = await bridge.listTabs()
      if (cancelled || cancelledRef.current) return
      const slug = props.targetSlug.toLowerCase()
      let onTarget = false
      let onLogin = false
      for (const tab of tabs) {
        const u = (tab.url || '').toLowerCase()
        const targetMatched =
          targetKind === 'people'
            ? u.includes(slug) && u.includes('/people')
            : u.includes(slug)
        if (targetMatched) {
          onTarget = true
          break
        }
        if (LOGIN_HINTS.some((hint) => u.includes(hint))) onLogin = true
      }
      if (onTarget) {
        setPhase('settling')
        return
      }
      setHint(onLogin ? 'login' : 'loading')
      window.setTimeout(tick, POLL_MS)
    }

    void (async () => {
      await ensureTabOnce()
      void tick()
    })()

    return () => {
      cancelled = true
    }
  }, [phase, props.targetSlug, props.targetUrl, targetKind])

  // Phase: login_required — the embedded panel is showing the LinkedIn login page.
  // Poll checkSession() every 2s; once JSESSIONID appears, re-run prepare() which
  // will find the complete session and navigate to the target URL.
  useEffect(() => {
    if (phase !== 'login_required') return
    let cancelled = false
    const bridge = window.beautifulLinkedIn?.embeddedBrowser
    if (!bridge) return

    const poll = async () => {
      if (cancelled || cancelledRef.current) return
      console.log('[ChromeBootstrapModal] login_required: verificando sessão…')
      const { hasJsessionid } = await bridge.checkSession()
      if (hasJsessionid && !cancelled && !cancelledRef.current) {
        console.log('[ChromeBootstrapModal] login_required: JSESSIONID detectado ✓ → escondendo painel e re-preparando')
        await bridge.hide()
        const result = await bridge.prepare(props.liAt?.trim() ?? '', props.targetUrl)
        console.log('[ChromeBootstrapModal] login_required: re-prepare result =', JSON.stringify(result))
        if (cancelled || cancelledRef.current) return
        if (result.ready && !result.onAuthwall) {
          setPhase('settling')
        } else if (result.needsLogin) {
          // Ainda precisa de login — reabre/foca a janela de login separada
          // (não a WebContentsView embutida, que perde input no Windows).
          await bridge.reloadLogin()
          window.setTimeout(poll, 3000)
        } else {
          setError(result.error ?? 'Falha após login.')
          setPhase('failed')
        }
        return
      }
      window.setTimeout(poll, 2000)
    }

    void poll()
    return () => {
      cancelled = true
    }
  }, [phase, props.liAt, props.targetUrl])

  useEffect(() => {
    if (phase !== 'settling') return
    setSettleProgress(0)
    const started = Date.now()
    const interval = window.setInterval(() => {
      const elapsed = Date.now() - started
      const pct = Math.min(100, Math.round((elapsed / settleMs) * 100))
      setSettleProgress(pct)
      if (elapsed >= settleMs) {
        window.clearInterval(interval)
        if (!cancelledRef.current) setPhase('ready')
      }
    }, 100)
    return () => window.clearInterval(interval)
  }, [phase, settleMs])

  useEffect(() => {
    if (phase !== 'ready') return
    cancelledRef.current = true
    props.onReady()
  }, [phase, props])

  const handleCancel = () => {
    cancelledRef.current = true
    props.onCancel()
  }

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') handleCancel()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  })

  return (
    <div className="sheet-overlay" role="dialog" aria-modal="true" aria-labelledby="chrome-bootstrap-title">
      <div
        className="card"
        style={{ width: 'min(460px, 100%)', borderRadius: 14, overflow: 'hidden' }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="card-body" style={{ padding: 22 }}>
          {renderHeader(phase, hint, targetKind)}
          {renderBody(phase, { error, settleProgress, hint, targetUrl: props.targetUrl, targetKind })}
          {renderFooter(phase, { onCancel: handleCancel })}
        </div>
      </div>
    </div>
  )
}

function renderHeader(phase: Phase, hint: 'loading' | 'login' | null, targetKind: 'people' | 'profile') {
  const targetLabel = targetKind === 'profile' ? 'perfil' : 'página da empresa'
  const actionLabel = targetKind === 'profile' ? 'validação' : 'busca'
  const titleMap: Record<Phase, { title: string; sub: string }> = {
    login_required: {
      title: 'Faça login no LinkedIn',
      sub: 'O cookie li_at não autenticou. Faça login na janela abaixo — a busca começa sozinha.'
    },
    preparing: {
      title: 'Abrindo LinkedIn…',
      sub: 'Carregando a página dentro do app — sem abrir o Chrome separado.'
    },
    checking: { title: 'Preparando Chrome…', sub: 'Verificando porta de debug 9222.' },
    launching: {
      title: 'Abrindo Chrome dedicado…',
      sub: 'Janela separada com perfil próprio (seu Chrome principal continua aberto).'
    },
    waiting_cdp: { title: 'Aguardando porta de debug…', sub: 'Conectando ao DevTools Protocol.' },
    awaiting_target:
      hint === 'login'
        ? {
            title: 'Logue no LinkedIn',
            sub: `Assim que você logar, o LinkedIn vai te redirecionar para o ${targetLabel}.`
          }
        : {
            title: `Aguardando ${targetLabel}…`,
            sub: `A ${actionLabel} começa assim que essa aba abrir.`
          },
    settling: {
      title: 'Página carregada. Aguardando 3s…',
      sub:
        targetKind === 'profile'
          ? 'Pequena pausa para a experiência e contatos renderizarem antes da coleta.'
          : 'Pequena pausa para a listagem renderizar antes da coleta.'
    },
    ready: { title: 'Tudo pronto', sub: `Iniciando ${actionLabel}.` },
    failed: { title: 'Não consegui preparar o Chrome', sub: 'Veja o erro abaixo.' }
  }
  const item = titleMap[phase]
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 14 }}>
      <span
        style={{
          display: 'grid',
          placeItems: 'center',
          width: 38,
          height: 38,
          borderRadius: 99,
          background: phase === 'failed' ? 'rgba(255,59,48,0.12)' : 'rgba(10,132,255,0.12)',
          color: phase === 'failed' ? 'var(--risky)' : 'var(--accent, #0a84ff)'
        }}
      >
        {phase === 'failed' ? '!' : phase === 'ready' ? '✓' : '◐'}
      </span>
      <div>
        <h3 id="chrome-bootstrap-title" style={{ margin: 0, fontSize: 16, fontWeight: 600, color: 'var(--ink)' }}>
          {item.title}
        </h3>
        <p style={{ margin: 0, fontSize: 12, color: 'var(--ink-3)' }}>{item.sub}</p>
      </div>
    </div>
  )
}

interface BodyContext {
  error: string | null
  settleProgress: number
  hint: 'loading' | 'login' | null
  targetUrl: string
  targetKind: 'people' | 'profile'
}

function renderBody(phase: Phase, ctx: BodyContext) {
  if (phase === 'failed') {
    return (
      <p style={{ margin: '4px 0 18px', fontSize: 13, color: 'var(--ink-2)' }}>
        {ctx.error ?? 'Erro desconhecido.'}
      </p>
    )
  }
  if (phase === 'login_required') {
    return (
      <div style={{ margin: '0 0 18px', fontSize: 13, color: 'var(--ink-2)', lineHeight: 1.55 }}>
        <p style={{ marginTop: 0 }}>
          O LinkedIn pediu login. Faça login na janela do LinkedIn que acabei de abrir.
          Quando terminar, a busca começa automaticamente.
        </p>
        <div style={{ marginTop: 10, display: 'flex', alignItems: 'center', gap: 10 }}>
          <span className="spinner" />
          <span style={{ fontSize: 12, color: 'var(--ink-3)' }}>Aguardando login…</span>
        </div>
      </div>
    )
  }
  if (phase === 'awaiting_target') {
    return (
      <div style={{ margin: '0 0 18px', fontSize: 13, color: 'var(--ink-2)', lineHeight: 1.55 }}>
        <p style={{ marginTop: 0 }}>
          {ctx.hint === 'login'
            ? `Faça login na janela do Chrome dedicado. Não preciso de cookie nem de cole-aqui — quando o LinkedIn carregar ${ctx.targetKind === 'profile' ? 'o perfil' : 'a página da empresa'}, ${ctx.targetKind === 'profile' ? 'a validação' : 'a busca'} começa sozinha.`
            : `O LinkedIn está carregando ${ctx.targetKind === 'profile' ? 'o perfil' : 'a página da empresa'} nesta janela do Chrome dedicado. Estou só observando — não vou abrir novas abas.`}
        </p>
        <div
          style={{
            marginTop: 10,
            padding: 8,
            borderRadius: 6,
            background: 'var(--surface-2)',
            fontFamily: 'var(--mono)',
            fontSize: 11,
            color: 'var(--ink-3)',
            wordBreak: 'break-all'
          }}
        >
          {ctx.targetUrl}
        </div>
        <div style={{ marginTop: 10, display: 'flex', alignItems: 'center', gap: 10 }}>
          <span className="spinner" />
          <span style={{ fontSize: 12, color: 'var(--ink-3)' }}>
            Verificando a cada {Math.round(POLL_MS / 1000)}s…
          </span>
        </div>
      </div>
    )
  }
  if (phase === 'settling') {
    return (
      <div style={{ margin: '0 0 18px' }}>
        <p style={{ marginTop: 0, fontSize: 13, color: 'var(--ink-2)' }}>
          Esperando 3 segundos antes de mandar {ctx.targetKind === 'profile' ? 'a validação' : 'a busca'}…
        </p>
        <div
          style={{
            height: 4,
            background: 'var(--surface-3, rgba(0,0,0,0.08))',
            borderRadius: 99,
            overflow: 'hidden'
          }}
        >
          <div
            style={{
              width: `${ctx.settleProgress}%`,
              height: '100%',
              background: 'var(--accent, #0a84ff)',
              transition: 'width 120ms linear'
            }}
          />
        </div>
      </div>
    )
  }
  return (
    <div style={{ margin: '0 0 18px', display: 'flex', alignItems: 'center', gap: 10 }}>
      <span className="spinner" />
      <span style={{ fontSize: 12, color: 'var(--ink-3)' }}>
        {phase === 'preparing' && 'Injetando sessão e carregando LinkedIn no app…'}
        {phase === 'checking' && 'Probing 127.0.0.1:9222…'}
        {phase === 'launching' && 'Spawn chrome.exe --remote-debugging-port=9222…'}
        {phase === 'waiting_cdp' && 'Aguardando resposta de /json/version…'}
        {phase === 'ready' && 'Iniciando…'}
      </span>
    </div>
  )
}

interface FooterContext {
  onCancel(): void
}

function renderFooter(phase: Phase, ctx: FooterContext) {
  if (phase === 'failed') {
    return (
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
        <button type="button" className="pill-btn" onClick={ctx.onCancel}>
          Fechar
        </button>
      </div>
    )
  }
  return (
    <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
      <button type="button" className="pill-btn" onClick={ctx.onCancel}>
        Cancelar
      </button>
    </div>
  )
}
