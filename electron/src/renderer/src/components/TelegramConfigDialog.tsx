import { useState } from 'react'

import { ApiClient } from '../../../shared/api'

interface Props {
  open: boolean
  client: ApiClient
  /** Fired after the credentials are persisted — the parent should advance
   *  to the phone-login step (TelethonAuthDialog). */
  onSaved(): void
  onClose(): void
}

const MY_TELEGRAM_URL = 'https://my.telegram.org/apps'

/**
 * Guided setup for the Telegram API credentials (api_id / api_hash).
 *
 * These identify the *application* to Telegram and are issued once, per
 * account, at my.telegram.org — there is no API to mint them, so the only
 * sane UX is to walk the operator through it and persist what they paste.
 * Once saved, the sidecar reports ``configured: true`` and the parent opens
 * the phone-login dialog (TelethonAuthDialog).
 */
export default function TelegramConfigDialog(props: Props): JSX.Element | null {
  const [apiId, setApiId] = useState('')
  const [apiHash, setApiHash] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  if (!props.open) return null

  const reset = (): void => {
    setApiId('')
    setApiHash('')
    setBusy(false)
    setError(null)
  }

  const close = (): void => {
    reset()
    props.onClose()
  }

  const submit = async (): Promise<void> => {
    const id = apiId.trim()
    const hash = apiHash.trim()
    if (!/^\d+$/.test(id)) {
      setError('O API ID deve conter apenas números (ex: 1234567).')
      return
    }
    if (!hash) {
      setError('Cole o API Hash (sequência de letras e números).')
      return
    }
    setBusy(true)
    setError(null)
    try {
      await props.client.saveTelethonConfig({ api_id: id, api_hash: hash })
      reset()
      props.onSaved()
    } catch (err) {
      setError(formatError(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      className="enrich-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="Configurar credenciais do Telegram"
    >
      <div className="enrich-overlay-backdrop" onClick={busy ? undefined : close} />
      <div className="enrich-modal" data-running={busy ? 'true' : 'false'}>
        <header className="enrich-modal-header">
          <div className="enrich-modal-title">
            <strong>Configurar Telegram</strong>
            <span className="enrich-modal-subtitle">
              Etapa 1 de 2 — credenciais da API
            </span>
          </div>
          <button
            type="button"
            className="enrich-modal-close"
            onClick={close}
            disabled={busy}
          >
            ×
          </button>
        </header>

        <div className="enrich-modal-body">
          <p>
            Para habilitar as consultas, o Telegram exige uma credencial de
            aplicativo (gratuita) gerada na sua conta. É rápido e só precisa ser
            feito uma vez.
          </p>

          <ol className="telegram-config-steps">
            <li>
              Abra o painel de aplicativos do Telegram e faça login com o seu
              número:
              <a
                className="enrich-modal-primary telegram-config-link"
                href={MY_TELEGRAM_URL}
                target="_blank"
                rel="noreferrer"
              >
                Abrir my.telegram.org ↗
              </a>
            </li>
            <li>
              Clique em <strong>API development tools</strong>. Se for a primeira
              vez, preencha <em>App title</em> e <em>Short name</em> com qualquer
              nome (ex: <code>beautiful</code>) — os outros campos podem ficar em
              branco — e confirme em <strong>Create application</strong>.
            </li>
            <li>
              Copie os valores <strong>App api_id</strong> e{' '}
              <strong>App api_hash</strong> e cole abaixo.
            </li>
          </ol>

          <div className="virtual-number-tip" role="note">
            <span className="virtual-number-tip__icon" aria-hidden="true">💡</span>
            <span>
              Se a página retornar um erro ao criar o aplicativo, aguarde alguns
              minutos e tente de novo — o site do Telegram costuma falhar de
              forma intermitente nesse passo.
            </span>
          </div>

          <label className="enrich-modal-field">
            <span>API ID</span>
            <input
              type="text"
              inputMode="numeric"
              value={apiId}
              onChange={(e) => setApiId(e.target.value)}
              placeholder="1234567"
              disabled={busy}
              autoFocus
            />
          </label>
          <label className="enrich-modal-field">
            <span>API Hash</span>
            <input
              type="text"
              value={apiHash}
              onChange={(e) => setApiHash(e.target.value)}
              placeholder="0123456789abcdef0123456789abcdef"
              disabled={busy}
            />
          </label>

          {error && <div className="enrich-modal-error">{error}</div>}
        </div>

        <footer className="enrich-modal-footer">
          <button
            type="button"
            className="enrich-modal-secondary"
            onClick={close}
            disabled={busy}
          >
            Cancelar
          </button>
          <button
            type="button"
            className="enrich-modal-primary"
            onClick={submit}
            disabled={busy}
          >
            {busy ? 'Salvando...' : 'Salvar e continuar'}
          </button>
        </footer>
      </div>
    </div>
  )
}

function formatError(err: unknown): string {
  if (err instanceof Error) return err.message
  if (typeof err === 'string') return err
  return 'Não foi possível salvar as credenciais.'
}
