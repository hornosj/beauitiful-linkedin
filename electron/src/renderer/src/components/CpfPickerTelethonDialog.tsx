import { useEffect, useRef, useState } from 'react'

import { ApiClient, ApiError } from '../../../shared/api'
import type {
  TelegramPhoneRankedCandidate,
  TelethonPipelineResponse
} from '../../../shared/types'
import { CpfReviewList } from './CpfReviewList'

interface Props {
  open: boolean
  client: ApiClient | null
  tableId: string | null
  leadRef: string
  leadName: string | null
  candidates: TelegramPhoneRankedCandidate[]
  eligibleCpfs: string[]
  onClose(): void
  onResult(response: TelethonPipelineResponse): void
  onAuthRequired(cpfs: string[]): void
  onError(message: string): void
}

/**
 * Modal de revisão de CPFs (tela "Revisar CPFs" do mock sinais.png),
 * dedicada ao caminho **Telethon-only**. O dialog NUNCA aciona Chrome
 * via CDP nem o pipeline Playwright — para cada CPF selecionado
 * dispara ``telegramPhoneTelethonCpfStage`` sequencialmente.
 *
 * Os CPFs aqui já vêm dos eventos `telegram-consult` (tipicamente
 * persistidos pelo "Pipeline Telethon"). O dialog só os exibe e
 * coordena a etapa de telefone.
 */
export default function CpfPickerTelethonDialog(props: Props): JSX.Element | null {
  const {
    open,
    client,
    tableId,
    leadRef,
    leadName,
    candidates,
    eligibleCpfs,
    onClose,
    onResult,
    onAuthRequired,
    onError
  } = props
  const [busy, setBusy] = useState(false)
  const [progressLabel, setProgressLabel] = useState<string | null>(null)
  // O modal pode ser fechado enquanto a consulta ainda roda: o loop em
  // `handleConfirm` é uma promise em voo que continua vivendo após o
  // desmonte (os callbacks `onResult`/`onError`/`onAuthRequired` são do
  // pai, então o resultado ainda chega). Esta ref evita `setState` em
  // componente desmontado depois que o operador manda a consulta pro
  // background pelo ✕.
  const mountedRef = useRef(true)
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  useEffect(() => {
    if (!open) {
      setBusy(false)
      setProgressLabel(null)
    }
  }, [open])

  if (!open) return null

  const handleConfirm = async (cpfs: string[]): Promise<void> => {
    if (!client || !tableId || cpfs.length === 0) return
    setBusy(true)
    let lastResponse: TelethonPipelineResponse | null = null
    let authRequired = false
    try {
      for (let index = 0; index < cpfs.length; index += 1) {
        const cpf = cpfs[index]
        if (mountedRef.current) setProgressLabel(`Consultando ${index + 1}/${cpfs.length}…`)
        try {
          lastResponse = await client.telegramPhoneTelethonCpfStage(tableId, {
            lead_ref: leadRef,
            cpf
          })
        } catch (error) {
          if (error instanceof ApiError && isTelethonAuthError(error)) {
            authRequired = true
            break
          }
          throw error
        }
      }
      if (authRequired) {
        onAuthRequired(cpfs)
      } else if (lastResponse) {
        onResult(lastResponse)
      }
    } catch (error) {
      onError(formatError(error))
    } finally {
      if (mountedRef.current) {
        setBusy(false)
        setProgressLabel(null)
      }
    }
  }

  const handleSkip = (): void => {
    if (busy) return
    onClose()
  }

  return (
    <div
      className="enrich-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="Revisar CPFs encontrados"
    >
      <div className="enrich-overlay-backdrop" onClick={onClose} />
      <div className="enrich-modal wide" data-running={busy ? 'true' : 'false'}>
        <header className="enrich-modal-header">
          <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10 }}>
            <div className="enrich-modal-title">
              <span
                className="enrich-modal-dot"
                data-state={busy ? 'live' : 'idle'}
                aria-hidden="true"
              />
              <h3>Revisar CPFs — {leadName ?? leadRef}</h3>
            </div>
            <button
              type="button"
              className="enrich-modal-close"
              onClick={onClose}
              title={
                busy
                  ? 'Fechar — a consulta continua rodando em segundo plano'
                  : 'Fechar'
              }
              aria-label="Fechar"
            >
              ×
            </button>
          </div>
          <p className="enrich-modal-sub">
            A consulta encontrou os CPFs abaixo. Desmarque os que você não quer
            consultar e clique em &quot;Buscar telefones&quot;. A consulta é feita
            inteiramente pela sessão nativa do Telegram — nenhum Chrome será aberto.
          </p>
          {busy && (
            <p className="enrich-modal-sub" style={{ color: 'var(--ink-3)' }}>
              Pode fechar esta janela no ✕ — a consulta continua rodando em segundo
              plano e o resultado aparece quando terminar.
            </p>
          )}
          {progressLabel && (
            <p className="enrich-modal-sub" style={{ color: 'var(--ink-2)' }}>
              {progressLabel}
            </p>
          )}
        </header>
        <div className="enrich-modal-body">
          <CpfReviewList
            candidates={candidates}
            defaultSelected={eligibleCpfs}
            leadName={leadName}
            leadRef={leadRef}
            onConfirm={handleConfirm}
            onSkip={handleSkip}
            busy={busy}
          />
        </div>
      </div>
    </div>
  )
}

function isTelethonAuthError(error: ApiError): boolean {
  const body = error.body
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail?: unknown }).detail
    if (typeof detail === 'string') {
      return detail.includes('telethon_session_not_authorized')
    }
  }
  return error.message.includes('telethon_session_not_authorized')
}

function formatError(error: unknown): string {
  if (error instanceof ApiError) return `${error.status}: ${error.message}`
  if (error instanceof Error) return error.message
  return 'Erro desconhecido na consulta.'
}
