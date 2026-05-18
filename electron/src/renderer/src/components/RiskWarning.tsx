import { useEffect } from 'react'

interface Props {
  onCancel(): void
  onConfirm(): void
}

export default function RiskWarning(props: Props) {
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') props.onCancel()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [props])

  return (
    <div
      className="sheet-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="risk-warning-title"
      onClick={(e) => {
        if (e.target === e.currentTarget) props.onCancel()
      }}
    >
      <div
        className="card"
        style={{ width: 'min(440px, 100%)', borderRadius: 14, overflow: 'hidden' }}
      >
        <div className="card-body" style={{ padding: 22 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 14 }}>
            <span
              style={{
                display: 'grid',
                placeItems: 'center',
                width: 38,
                height: 38,
                borderRadius: 99,
                background: 'rgba(255,59,48,0.12)',
                color: 'var(--risky)'
              }}
            >
              <svg
                width="20"
                height="20"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z" />
                <path d="M12 9v4" />
                <path d="M12 17h.01" />
              </svg>
            </span>
            <div>
              <h3 id="risk-warning-title" style={{ margin: 0, fontSize: 16, fontWeight: 600, color: 'var(--ink)' }}>
                Modo arriscado
              </h3>
              <p style={{ margin: 0, fontSize: 12, color: 'var(--ink-3)' }}>
                Playwright logado no LinkedIn
              </p>
            </div>
          </div>

          <p style={{ margin: '0 0 12px', fontSize: 13, color: 'var(--ink-2)' }}>
            Este modo navega no LinkedIn com a sua conta. Existe risco real de{' '}
            <strong>restrição temporária ou banimento permanente</strong>.
          </p>

          <ul
            style={{
              margin: '0 0 18px',
              padding: 0,
              listStyle: 'none',
              fontSize: 12,
              color: 'var(--ink-2)',
              lineHeight: 1.6
            }}
          >
            <li>• Use, de preferência, uma conta secundária.</li>
            <li>• Evite paralelismo e respeite os intervalos.</li>
            <li>• Pare imediatamente se aparecer captcha.</li>
          </ul>

          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
            <button type="button" className="pill-btn" onClick={props.onCancel} autoFocus>
              Cancelar
            </button>
            <button
              type="button"
              className="pill-btn"
              style={{
                background: 'var(--risky)',
                color: 'white',
                borderColor: 'transparent'
              }}
              onClick={props.onConfirm}
            >
              Aceito o risco
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
