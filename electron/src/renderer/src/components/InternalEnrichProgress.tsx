import type {
  InternalEnrichLeadEvent,
  InternalEnrichField,
  InternalEnrichPhase,
  InternalEnrichStreamEvent,
  InternalEnrichSummary
} from '../../../shared/types'

interface Props {
  open: boolean
  fields?: InternalEnrichField
  totalLeads: number
  running: boolean
  phase: InternalEnrichPhase | null
  completed: number
  domainsDone: number
  uniqueDomains: number
  recentLeads: InternalEnrichLeadEvent[]
  summary: InternalEnrichSummary | null
  errorMessage: string | null
  onCancel(): void
  onClose(): void
}

const PHASE_LABEL: Record<InternalEnrichPhase, string> = {
  discovering: 'Descobrindo domínios irmãos (CT, SPF/DMARC, ccTLD)',
  harvesting: 'Coletando contatos publicados nas páginas das empresas',
  lookup: 'Buscando telefones individuais em SERPs públicas',
  validating: 'Validando contatos (MX, SMTP, WhatsApp, formato)',
  completed: 'Concluído',
  cancelled: 'Cancelado'
}

const STATUS_LABEL: Record<InternalEnrichLeadEvent['status'], string> = {
  enriched: 'novo contato',
  skipped_existing_email: 'já tinha e-mail',
  skipped_existing_phone: 'já tinha telefone',
  failed_missing_domain: 'sem domínio',
  failed_no_candidate: 'sem candidato',
  failed_existing_phone: 'já tinha telefone',
  failed: 'sem candidato válido',
  no_change: 'sem mudança'
}

const STATUS_COLOR: Record<InternalEnrichLeadEvent['status'], string> = {
  enriched: 'var(--success)',
  skipped_existing_email: 'var(--ink-3)',
  skipped_existing_phone: 'var(--ink-3)',
  failed_missing_domain: 'var(--warn)',
  failed_no_candidate: 'var(--warn)',
  failed_existing_phone: 'var(--ink-3)',
  failed: 'var(--risky)',
  no_change: 'var(--ink-3)'
}

export default function InternalEnrichProgress(props: Props) {
  if (!props.open) return null

  const {
    fields = 'email',
    totalLeads,
    running,
    phase,
    completed,
    domainsDone,
    uniqueDomains,
    recentLeads,
    summary,
    errorMessage
  } = props

  const progress = totalLeads > 0 ? Math.min(1, completed / totalLeads) : 0
  const harvestProgress =
    uniqueDomains > 0 ? Math.min(1, domainsDone / uniqueDomains) : 0
  const showHarvestBar = phase === 'harvesting' || harvestProgress > 0
  const phaseLabel = phase ? PHASE_LABEL[phase] : 'Preparando…'

  return (
    <div
      className="enrich-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="Enriquecimento interno em andamento"
    >
      <div className="enrich-overlay-backdrop" onClick={props.onClose} />
      <div className="enrich-modal" data-running={running ? 'true' : 'false'}>
        <header className="enrich-modal-header">
          <div className="enrich-modal-title">
            <span
              className="enrich-modal-dot"
              data-state={running ? 'live' : summary ? 'done' : 'idle'}
              aria-hidden="true"
            />
            <h3>
              {running
                ? runningTitle(fields)
                : summary
                  ? 'Enriquecimento concluído'
                  : errorMessage
                    ? 'Algo deu errado'
                    : 'Enriquecimento interno'}
            </h3>
          </div>
          <p className="enrich-modal-sub">
            {running
              ? phaseLabel
              : summary
                ? buildSummaryHeadline(summary)
                : errorMessage ?? 'Sem APIs pagas. Inferência por padrão + MX + SMTP conservador.'}
          </p>
        </header>

        <div className="enrich-modal-body">
          <div className="enrich-progress-stack">
            {showHarvestBar && (
              <ProgressBar
                label={`Domínios analisados`}
                metric={`${domainsDone}/${uniqueDomains}`}
                value={harvestProgress}
                muted={phase !== 'harvesting'}
              />
            )}
            <ProgressBar
              label="Leads validados"
              metric={`${completed}/${totalLeads}`}
              value={progress}
              muted={phase === 'harvesting'}
              indeterminate={running && phase !== 'harvesting' && completed === 0}
            />
          </div>

          {recentLeads.length > 0 && (
            <ul className="enrich-lead-feed" aria-label="Últimos leads processados">
              {recentLeads.slice(0, 6).map((event, index) => {
                const domainTrace = formatDomainTrace(event)
                const foundContact = event.email ?? event.phone ?? null
                return (
                  <li
                    key={`${event.lead_ref ?? event.person_name ?? 'lead'}-${index}`}
                    className="enrich-lead-row"
                    data-status={event.status}
                    style={{ animationDelay: `${Math.min(index * 30, 120)}ms` }}
                    title={domainTrace ?? undefined}
                  >
                    <span className="enrich-lead-status-orb" aria-hidden="true">
                      <span
                        className="enrich-lead-status-dot"
                        style={{ background: STATUS_COLOR[event.status] }}
                      />
                    </span>
                    <div className="enrich-lead-meta">
                      <span className="enrich-lead-name">
                        {event.person_name ?? 'Lead sem nome'}
                      </span>
                      <span className="enrich-lead-subline">
                        {event.company_name && (
                          <span className="enrich-lead-company">{event.company_name}</span>
                        )}
                        {domainTrace && (
                          <span className="enrich-lead-domains" aria-label="Domínios testados">
                            {domainTrace}
                          </span>
                        )}
                      </span>
                    </div>
                    <span
                      className="enrich-lead-result"
                      style={{ color: STATUS_COLOR[event.status] }}
                    >
                      {foundContact ? (
                        <>
                          <span className="enrich-found-badge">Encontrado</span>
                          <span>{foundContact}</span>
                        </>
                      ) : (
                        STATUS_LABEL[event.status]
                      )}
                    </span>
                  </li>
                )
              })}
            </ul>
          )}

          {summary && <SummaryGrid summary={summary} />}
          {errorMessage && (
            <p className="enrich-error-message" role="alert">
              {errorMessage}
            </p>
          )}
        </div>

        <footer className="enrich-modal-footer">
          {running ? (
            <button type="button" className="enrich-btn ghost" onClick={props.onCancel}>
              Cancelar
            </button>
          ) : (
            <button
              type="button"
              className="enrich-btn primary"
              onClick={props.onClose}
              autoFocus
            >
              Fechar
            </button>
          )}
        </footer>
      </div>
    </div>
  )
}

function runningTitle(fields: InternalEnrichField): string {
  if (fields === 'phone') return 'Procurando telefones'
  if (fields === 'both') return 'Procurando contatos'
  return 'Procurando e-mails profissionais'
}

function ProgressBar(props: {
  label: string
  metric: string
  value: number
  muted?: boolean
  indeterminate?: boolean
}) {
  return (
    <div className="enrich-progress" data-muted={props.muted ? 'true' : 'false'}>
      <div className="enrich-progress-header">
        <span className="enrich-progress-label">{props.label}</span>
        <span className="enrich-progress-metric">{props.metric}</span>
      </div>
      <div
        className="enrich-progress-track"
        data-indeterminate={props.indeterminate ? 'true' : 'false'}
      >
        <div
          className="enrich-progress-fill"
          style={{ transform: `scaleX(${Math.max(0.02, props.value)})` }}
        />
      </div>
    </div>
  )
}

function SummaryGrid(props: { summary: InternalEnrichSummary }) {
  const { summary } = props
  const stats: Array<{ label: string; value: number; tone: string }> = [
    { label: 'Novos e-mails', value: summary.enriched_leads, tone: 'success' },
    {
      label: 'Novos telefones',
      value: summary.enriched_phone_leads ?? 0,
      tone: 'success'
    },
    { label: 'Já tinham', value: summary.skipped_existing_email, tone: 'muted' },
    {
      label: 'Já tinham tel.',
      value: summary.skipped_existing_phone ?? 0,
      tone: 'muted'
    },
    { label: 'Sem domínio', value: summary.failed_missing_domain, tone: 'warn' },
    {
      label: 'Sem tel.',
      value: summary.failed_no_phone_candidate ?? 0,
      tone: 'warn'
    },
    { label: 'Sem mudança', value: summary.no_change, tone: 'muted' }
  ]
  return (
    <div className="enrich-summary-grid">
      {stats.map((stat) => (
        <div key={stat.label} className="enrich-summary-card" data-tone={stat.tone}>
          <span className="enrich-summary-value">{stat.value}</span>
          <span className="enrich-summary-label">{stat.label}</span>
        </div>
      ))}
    </div>
  )
}

function buildSummaryHeadline(summary: InternalEnrichSummary): string {
  const phones = summary.enriched_phone_leads ?? 0
  if (summary.enriched_leads === 0 && phones === 0) {
    return 'Nenhum contato novo foi encontrado. Verifique domínios, padrões e fontes públicas.'
  }
  const parts: string[] = []
  if (summary.enriched_leads > 0) {
    const noun = summary.enriched_leads === 1 ? 'e-mail novo' : 'e-mails novos'
    parts.push(`${summary.enriched_leads} ${noun}`)
  }
  if (phones > 0) {
    const noun = phones === 1 ? 'telefone novo' : 'telefones novos'
    parts.push(`${phones} ${noun}`)
  }
  return `${parts.join(' e ')} adicionados aos seus leads.`
}

/**
 * Human-readable trace of which domains the orchestrator tested for a
 * lead — drives the small subtitle under the name and the row tooltip.
 *
 * Skipped when the lead has only one tested domain (the obvious case)
 * or no domain info at all.
 *
 * Examples:
 *   tested=[acme.com, acme.io], chosen=acme.io →
 *     "testou acme.com, acme.io · ✓ acme.io"
 *   tested=[acme.com], chosen=null, no email → null (no trace shown)
 *   tested=[acme.com, acme.io], chosen=null →
 *     "tentou acme.com, acme.io"
 */
function formatDomainTrace(event: InternalEnrichLeadEvent): string | null {
  const tested = event.tested_domains ?? []
  if (tested.length === 0) return null
  if (tested.length === 1 && event.chosen_domain === tested[0]) return null

  const list = tested.join(', ')
  if (event.chosen_domain) {
    return `testou ${list} · ✓ ${event.chosen_domain}`
  }
  if (tested.length === 1) return null
  return `tentou ${list}`
}

// ---------------------------------------------------------------------------
// Reducer-style helper for the parent component
// ---------------------------------------------------------------------------

export interface ProgressState {
  open: boolean
  running: boolean
  phase: InternalEnrichPhase | null
  totalLeads: number
  completed: number
  uniqueDomains: number
  domainsDone: number
  /** Number of new domains the discovery phase surfaced across all companies. */
  discoveredDomains: number
  /** Counts grouped by discovery source — e.g. {"crt_sh": 4, "spf_dmarc": 1}. */
  discoveryBySource: Record<string, number>
  recentLeads: InternalEnrichLeadEvent[]
  summary: InternalEnrichSummary | null
  errorMessage: string | null
  /**
   * Phone pipeline progress, tracked separately from the e-mail
   * pipeline so ``fields="both"`` runs can render two progress lanes
   * without one side hiding the other.
   *
   * Events tagged ``channel: "phone"`` write here; un-tagged events
   * continue to update the legacy e-mail counters above (the existing
   * SSE contract).
   */
  phonePhase: InternalEnrichPhase | null
  phoneCompleted: number
  phoneDomainsDone: number
  /** Sum of SERP/lookup candidates returned across all leads. */
  phoneLookupCandidates: number
  /** Convenience counter: phones found so far for the live tail. */
  phonesFound: number
}

export const INITIAL_PROGRESS: ProgressState = {
  open: false,
  running: false,
  phase: null,
  totalLeads: 0,
  completed: 0,
  uniqueDomains: 0,
  domainsDone: 0,
  discoveredDomains: 0,
  discoveryBySource: {},
  recentLeads: [],
  summary: null,
  errorMessage: null,
  phonePhase: null,
  phoneCompleted: 0,
  phoneDomainsDone: 0,
  phoneLookupCandidates: 0,
  phonesFound: 0
}

export function reduceProgress(
  state: ProgressState,
  event: InternalEnrichStreamEvent
): ProgressState {
  // ``channel`` is the demultiplexer for ``fields="both"`` runs. Events
  // without a channel default to "email" — that's the historical
  // contract from before the phone pipeline existed and keeps the
  // existing UI working unchanged when the request only asked for
  // e-mail. Phone events arrive tagged ``channel: "phone"``.
  const channel = ('channel' in event ? event.channel : undefined) ?? 'email'

  switch (event.type) {
    case 'start':
      return {
        ...state,
        running: true,
        totalLeads: event.total,
        uniqueDomains: event.unique_domains,
        completed: 0,
        domainsDone: 0,
        discoveredDomains: 0,
        discoveryBySource: {},
        recentLeads: [],
        summary: null,
        errorMessage: null,
        phonePhase: null,
        phoneCompleted: 0,
        phoneDomainsDone: 0,
        phoneLookupCandidates: 0,
        phonesFound: 0
      }
    case 'phase':
      if (channel === 'phone') {
        return { ...state, phonePhase: event.phase }
      }
      return { ...state, phase: event.phase }
    case 'discovery': {
      // Discovery is e-mail-only today; ignore channel for now.
      const nextBySource = { ...state.discoveryBySource }
      for (const [source, items] of Object.entries(event.sources ?? {})) {
        nextBySource[source] = (nextBySource[source] ?? 0) + items.length
      }
      return {
        ...state,
        discoveredDomains: state.discoveredDomains + event.discovered,
        discoveryBySource: nextBySource,
        uniqueDomains: state.uniqueDomains + event.discovered
      }
    }
    case 'domain':
      if (channel === 'phone') {
        return { ...state, phoneDomainsDone: state.phoneDomainsDone + 1 }
      }
      return { ...state, domainsDone: state.domainsDone + 1 }
    case 'lookup':
      return {
        ...state,
        phoneLookupCandidates:
          state.phoneLookupCandidates + (event.candidates ?? 0)
      }
    case 'progress':
      if (channel === 'phone') {
        return {
          ...state,
          phoneCompleted: event.completed,
          totalLeads: event.total || state.totalLeads
        }
      }
      return {
        ...state,
        completed: event.completed,
        totalLeads: event.total || state.totalLeads
      }
    case 'lead': {
      const recent = [event, ...state.recentLeads].slice(0, 12)
      const phonesFound =
        channel === 'phone' && event.phone
          ? state.phonesFound + 1
          : state.phonesFound
      return { ...state, recentLeads: recent, phonesFound }
    }
    case 'done':
      return {
        ...state,
        running: false,
        phase: 'completed',
        phonePhase: state.phonePhase ? 'completed' : state.phonePhase,
        summary: event.summary,
        completed: state.totalLeads
      }
    case 'error':
      return {
        ...state,
        running: false,
        errorMessage: event.message
      }
    default:
      return state
  }
}
