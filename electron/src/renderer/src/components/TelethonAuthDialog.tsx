import { useState } from 'react'

import { ApiClient } from '../../../shared/api'

interface Props {
  open: boolean
  client: ApiClient
  onSuccess(): void
  onClose(): void
  /** Optional: lets the operator forget the saved api_id/api_hash and go
   *  back to the credentials tutorial when login keeps failing. */
  onReconfigure?(): void
}

type Step = 'phone' | 'code' | 'password'

const TELETHON_AUTH_TIMEOUT_MS = 30_000
const TELETHON_AUTH_TIMEOUT_MESSAGE =
  'Tempo de resposta do Telegram expirou. Tente novamente em alguns segundos.'

/**
 * Two-step Telegram login for the Telethon-backed evidence flow.
 *
 *   1. Operator types phone (E.164) → backend sends auth code via MTProto.
 *   2. Operator types code received in Telegram. If 2FA is enabled, the
 *      backend asks for the password on the next step.
 *
 * Saves the authenticated session in the sidecar's `data/` folder so the
 * Telethon consult flow stops failing with EOFError.
 */
export default function TelethonAuthDialog(props: Props): JSX.Element | null {
  const [step, setStep] = useState<Step>('phone')
  const [phone, setPhone] = useState('')
  const [phoneCodeHash, setPhoneCodeHash] = useState('')
  const [code, setCode] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  if (!props.open) return null

  const reset = (): void => {
    setStep('phone')
    setPhone('')
    setPhoneCodeHash('')
    setCode('')
    setPassword('')
    setBusy(false)
    setError(null)
  }

  const close = (): void => {
    reset()
    props.onClose()
  }

  const submitPhone = async (): Promise<void> => {
    const trimmed = phone.trim()
    if (!trimmed) {
      setError('Informe o telefone com DDI (ex: +5511999999999).')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const response = await withTimeout(
        props.client.sendTelethonAuthCode({ phone: trimmed }),
        TELETHON_AUTH_TIMEOUT_MESSAGE
      )
      setPhoneCodeHash(response.phone_code_hash)
      setStep('code')
    } catch (err) {
      setError(formatError(err))
    } finally {
      setBusy(false)
    }
  }

  const submitCode = async (passwordValue?: string): Promise<void> => {
    const trimmedCode = code.trim()
    if (!trimmedCode) {
      setError('Informe o código recebido no Telegram.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const response = await withTimeout(
        props.client.signInTelethonAuth({
          phone: phone.trim(),
          phone_code_hash: phoneCodeHash,
          code: trimmedCode,
          password: passwordValue ?? null
        }),
        TELETHON_AUTH_TIMEOUT_MESSAGE
      )
      if (response.requires_password) {
        setStep('password')
        setBusy(false)
        return
      }
      if (response.authorized) {
        reset()
        props.onSuccess()
        return
      }
      setError('Login não confirmado pelo Telegram. Tente novamente.')
    } catch (err) {
      setError(formatError(err))
    } finally {
      setBusy(false)
    }
  }

  const submitPassword = async (): Promise<void> => {
    const trimmed = password.trim()
    if (!trimmed) {
      setError('Informe a senha do verificador 2FA.')
      return
    }
    await submitCode(trimmed)
  }

  return (
    <div
      className="enrich-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="Login Telegram via Telethon"
    >
      <div className="enrich-overlay-backdrop" onClick={busy ? undefined : close} />
      <div className="enrich-modal" data-running={busy ? 'true' : 'false'}>
        <header className="enrich-modal-header">
          <div className="enrich-modal-title">
            <strong>Login Telegram (Telethon)</strong>
            <span className="enrich-modal-subtitle">
              {step === 'phone' && 'Etapa 1 de 2 — telefone'}
              {step === 'code' && 'Etapa 2 de 2 — código'}
              {step === 'password' && 'Verificação em duas etapas'}
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
          {step === 'phone' && (
            <>
              <p>
                Autentique uma conta Telegram para habilitar as consultas. Informe o
                telefone com DDI (ex: <code>+5511999999999</code>). Você receberá um
                código no Telegram.
              </p>
              <div className="virtual-number-tip" role="note">
                <span className="virtual-number-tip__icon" aria-hidden="true">💡</span>
                <span>
                  <strong>Recomendação:</strong> use um número virtual ou secundário
                  para esta aplicação. As consultas envolvem grupos e serviços de
                  terceiros — manter tudo em um número dedicado deixa sua conta
                  pessoal organizada e protegida. É só uma boa prática, não um
                  requisito.
                </span>
              </div>
              <label className="enrich-modal-field">
                <span>Telefone</span>
                <input
                  type="tel"
                  value={phone}
                  onChange={(e) => setPhone(e.target.value)}
                  placeholder="+5511999999999"
                  disabled={busy}
                  autoFocus
                />
              </label>
              {props.onReconfigure && (
                <button
                  type="button"
                  className="enrich-modal-linkbtn"
                  onClick={props.onReconfigure}
                  disabled={busy}
                >
                  Usar outras credenciais de API
                </button>
              )}
            </>
          )}

          {step === 'code' && (
            <>
              <p>
                Enviamos um código para <strong>{phone}</strong> via Telegram. Cole
                aqui — geralmente são 5 dígitos.
              </p>
              <label className="enrich-modal-field">
                <span>Código</span>
                <input
                  type="text"
                  inputMode="numeric"
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                  placeholder="12345"
                  disabled={busy}
                  autoFocus
                />
              </label>
            </>
          )}

          {step === 'password' && (
            <>
              <p>
                Sua conta tem verificação em duas etapas habilitada. Informe a senha
                cadastrada no Telegram.
              </p>
              <label className="enrich-modal-field">
                <span>Senha 2FA</span>
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  disabled={busy}
                  autoFocus
                />
              </label>
            </>
          )}

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
          {step === 'phone' && (
            <button
              type="button"
              className="enrich-modal-primary"
              onClick={submitPhone}
              disabled={busy}
            >
              {busy ? 'Enviando...' : 'Enviar código'}
            </button>
          )}
          {step === 'code' && (
            <button
              type="button"
              className="enrich-modal-primary"
              onClick={() => submitCode()}
              disabled={busy}
            >
              {busy ? 'Confirmando...' : 'Confirmar código'}
            </button>
          )}
          {step === 'password' && (
            <button
              type="button"
              className="enrich-modal-primary"
              onClick={submitPassword}
              disabled={busy}
            >
              {busy ? 'Confirmando...' : 'Confirmar senha'}
            </button>
          )}
        </footer>
      </div>
    </div>
  )
}

function formatError(err: unknown): string {
  if (err instanceof Error) return err.message
  if (typeof err === 'string') return err
  return 'Erro desconhecido.'
}

function withTimeout<T>(promise: Promise<T>, message: string): Promise<T> {
  let timeoutId: number | undefined
  const timeout = new Promise<never>((_, reject) => {
    timeoutId = window.setTimeout(() => reject(new Error(message)), TELETHON_AUTH_TIMEOUT_MS)
  })
  return Promise.race([promise, timeout]).finally(() => {
    if (timeoutId !== undefined) window.clearTimeout(timeoutId)
  })
}
