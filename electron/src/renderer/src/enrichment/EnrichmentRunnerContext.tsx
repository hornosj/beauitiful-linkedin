import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode
} from 'react'

import { ApiClient } from '../../../shared/api'
import type {
  InternalEnrichStreamDoneEvent,
  InternalEnrichStreamEvent
} from '../../../shared/types'
import {
  INITIAL_PROGRESS,
  reduceProgress,
  type ProgressState
} from '../components/InternalEnrichProgress'

/**
 * Global runner for internal enrichment.
 *
 * The run lives at the App level so the user can keep navigating
 * (Nova busca, Configurações, etc.) while validation continues in the
 * background. A persistent chip surfaces progress when the modal is
 * closed; subscribers (e.g. SavedLeadsLibrary) can listen for the
 * ``completed`` event to refresh their cached leads.
 */
export interface EnrichmentRunMeta {
  tableId: string
  tableName: string
  startedAt: number
}

export interface EnrichmentRunState extends ProgressState {
  meta: EnrichmentRunMeta
  doneEvent: InternalEnrichStreamDoneEvent | null
}

export type CompletionListener = (
  meta: EnrichmentRunMeta,
  done: InternalEnrichStreamDoneEvent
) => void

export interface StartEnrichmentArgs {
  tableId: string
  tableName: string
  leadRefs?: string[]
  totalLeads: number
  companyDomain?: string | null
}

export interface EnrichmentRunnerContextValue {
  run: EnrichmentRunState | null
  modalOpen: boolean
  start(args: StartEnrichmentArgs): boolean // false if a run is already in flight
  cancel(): void
  dismiss(): void // clears finished run, closes modal
  openModal(): void
  closeModal(): void // hide modal without stopping the run
  onCompleted(listener: CompletionListener): () => void
}

const EnrichmentRunnerContext = createContext<EnrichmentRunnerContextValue | null>(
  null
)

interface ProviderProps {
  client: ApiClient | null
  onError?(message: string): void
  onSuccess?(message: string): void
  children: ReactNode
}

export function EnrichmentRunnerProvider(props: ProviderProps) {
  const { client, onError, onSuccess, children } = props

  const [run, setRun] = useState<EnrichmentRunState | null>(null)
  const [modalOpen, setModalOpen] = useState(false)

  const abortRef = useRef<AbortController | null>(null)
  const listenersRef = useRef<Set<CompletionListener>>(new Set())

  const applyEvent = useCallback((event: InternalEnrichStreamEvent) => {
    setRun((prev) => {
      if (!prev) return prev
      const nextProgress = reduceProgress(prev, event)
      const doneEvent =
        event.type === 'done' ? event : prev.doneEvent
      return { ...prev, ...nextProgress, doneEvent }
    })
  }, [])

  const start = useCallback(
    (args: StartEnrichmentArgs) => {
      if (!client) {
        onError?.('Servidor indisponível.')
        return false
      }
      if (run?.running) {
        onError?.(
          'Já existe um enriquecimento em andamento. Aguarde ou cancele para iniciar outro.'
        )
        return false
      }
      const meta: EnrichmentRunMeta = {
        tableId: args.tableId,
        tableName: args.tableName,
        startedAt: Date.now()
      }
      const controller = new AbortController()
      abortRef.current = controller
      const initial: EnrichmentRunState = {
        ...INITIAL_PROGRESS,
        open: false,
        running: true,
        totalLeads: args.totalLeads,
        meta,
        doneEvent: null
      }
      setRun(initial)
      setModalOpen(false)

      void (async () => {
        try {
          const done = await client.streamInternalEnrich(
            args.tableId,
            {
              lead_refs: args.leadRefs,
              fields: 'email',
              confirmed: true,
              company_domain: args.companyDomain ?? null
            },
            applyEvent,
            controller.signal
          )
          // The reducer also handles the 'done' event, but we set the
          // doneEvent explicitly here so listeners always see a fully
          // resolved payload before they react.
          setRun((prev) =>
            prev ? { ...prev, running: false, doneEvent: done, summary: done.summary } : prev
          )
          listenersRef.current.forEach((listener) => {
            try {
              listener(meta, done)
            } catch {
              /* listener errors must not break the runner */
            }
          })
          const s = done.summary
          onSuccess?.(
            `Enriquecimento concluído: ${s.enriched_leads} novo(s), ` +
              `${s.skipped_existing_email} já tinha e-mail, ` +
              `${s.failed_missing_domain} sem domínio.`
          )
        } catch (err) {
          if (controller.signal.aborted) {
            setRun((prev) =>
              prev
                ? { ...prev, running: false, errorMessage: 'Cancelado pelo usuário.' }
                : prev
            )
          } else {
            const message =
              err instanceof Error ? err.message : 'Falha ao enriquecer.'
            setRun((prev) =>
              prev ? { ...prev, running: false, errorMessage: message } : prev
            )
            onError?.(message)
          }
        } finally {
          if (abortRef.current === controller) {
            abortRef.current = null
          }
        }
      })()

      return true
    },
    [client, run, applyEvent, onError, onSuccess]
  )

  const cancel = useCallback(() => {
    abortRef.current?.abort()
  }, [])

  const dismiss = useCallback(() => {
    abortRef.current?.abort()
    abortRef.current = null
    setRun(null)
    setModalOpen(false)
  }, [])

  const openModal = useCallback(() => setModalOpen(true), [])
  const closeModal = useCallback(() => setModalOpen(false), [])

  const onCompleted = useCallback((listener: CompletionListener) => {
    listenersRef.current.add(listener)
    return () => {
      listenersRef.current.delete(listener)
    }
  }, [])

  const value = useMemo<EnrichmentRunnerContextValue>(
    () => ({
      run,
      modalOpen,
      start,
      cancel,
      dismiss,
      openModal,
      closeModal,
      onCompleted
    }),
    [run, modalOpen, start, cancel, dismiss, openModal, closeModal, onCompleted]
  )

  return (
    <EnrichmentRunnerContext.Provider value={value}>
      {children}
    </EnrichmentRunnerContext.Provider>
  )
}

export function useEnrichmentRunner(): EnrichmentRunnerContextValue {
  const ctx = useContext(EnrichmentRunnerContext)
  if (!ctx) {
    throw new Error('useEnrichmentRunner requires <EnrichmentRunnerProvider>.')
  }
  return ctx
}
