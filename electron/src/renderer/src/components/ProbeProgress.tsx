import type { ProbeEvent } from '../../../shared/types'

interface Props {
  companyName: string
  events: ProbeEvent[]
  status: 'running' | 'completed' | 'failed'
  onCancel?(): void
}

interface DisplayedStep {
  key: string
  icon: string
  text: string
  detail?: string
}

const ICON_DOT = '·'
const ICON_DONE = '✓'
const ICON_WARN = '⚠'

export default function ProbeProgress(props: Props) {
  const { companyName, events, status } = props
  const steps = mapEventsToSteps(companyName, events)
  return (
    <div
      role="status"
      aria-live="polite"
      style={{
        marginTop: 12,
        padding: '12px 14px',
        borderRadius: 10,
        border: '0.5px solid var(--ink-5, rgba(0,0,0,0.10))',
        background: 'var(--surface, #fff)'
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          fontSize: 12,
          color: 'var(--ink-3)',
          marginBottom: 8
        }}
      >
        <strong style={{ color: 'var(--ink-1)', fontSize: 13, marginRight: 8 }}>
          Reconhecendo empresa
        </strong>
        {status === 'running' && <span className="spinner-dot">…</span>}
        {status === 'completed' && <span style={{ color: 'var(--success)' }}>concluído</span>}
        {status === 'failed' && <span style={{ color: 'var(--risky)' }}>falhou</span>}
        <span style={{ flex: 1 }} />
        {props.onCancel && status === 'running' && (
          <button type="button" className="pill-btn" onClick={props.onCancel}>
            Cancelar
          </button>
        )}
      </div>
      <ol
        style={{
          listStyle: 'none',
          padding: 0,
          margin: 0,
          display: 'flex',
          flexDirection: 'column',
          gap: 4
        }}
      >
        {steps.map((step) => (
          <li
            key={step.key}
            style={{
              display: 'flex',
              alignItems: 'baseline',
              gap: 8,
              fontSize: 12,
              color: 'var(--ink-2)'
            }}
          >
            <span style={{ width: 14, color: 'var(--ink-3)' }}>{step.icon}</span>
            <span>{step.text}</span>
            {step.detail && (
              <span style={{ color: 'var(--ink-3)', fontFamily: 'var(--mono, monospace)' }}>
                {step.detail}
              </span>
            )}
          </li>
        ))}
      </ol>
    </div>
  )
}

function mapEventsToSteps(companyName: string, events: ProbeEvent[]): DisplayedStep[] {
  const steps: DisplayedStep[] = []
  for (const ev of events) {
    switch (ev.event) {
      case 'recognizing':
        steps.push({
          key: 'recognizing',
          icon: ICON_DOT,
          text: `Buscando empresa "${(ev.data.company_name as string) ?? companyName}"…`
        })
        break
      case 'recognized':
        steps.push({
          key: 'recognized',
          icon: ICON_DONE,
          text: 'Empresa reconhecida',
          detail: `/company/${ev.data.slug as string}/`
        })
        break
      case 'fetching':
        steps.push({
          key: 'fetching',
          icon: ICON_DOT,
          text: 'Abrindo a aba People no navegador conectado…'
        })
        break
      case 'employees_seen': {
        const count = ev.data.count as number | null
        const raw = ev.data.raw_text as string | null
        steps.push({
          key: 'employees_seen',
          icon: ICON_DONE,
          text: count != null ? `${count} funcionários associados` : 'Funcionários detectados',
          detail: raw ?? undefined
        })
        break
      }
      case 'cards_seen':
        steps.push({
          key: 'cards_seen',
          icon: ICON_DOT,
          text: `${ev.data.count as number} cards visíveis na busca`,
          detail: 'estimativa por heurística'
        })
        break
      case 'classified': {
        const isSmall = ev.data.is_small as boolean | null
        const source = ev.data.source as string
        const employees = ev.data.employee_count as number | null
        let text: string
        if (isSmall === true) {
          text = 'Empresa pequena reconhecida'
        } else if (isSmall === false) {
          text = 'Empresa de médio/grande porte'
        } else {
          text = 'Tamanho não pôde ser determinado'
        }
        steps.push({
          key: 'classified',
          icon: isSmall === null ? ICON_WARN : ICON_DONE,
          text,
          detail: [employees != null ? `~${employees} pessoas` : null, `fonte: ${source}`]
            .filter(Boolean)
            .join(' · ')
        })
        break
      }
      case 'login_required':
        steps.push({
          key: 'login_required',
          icon: ICON_WARN,
          text: 'LinkedIn pediu login — faça login no Chrome aberto e tente de novo.'
        })
        break
      case 'cdp_offline':
      case 'cdp_disabled':
      case 'playwright_unavailable':
      case 'cdp_connect_failed':
      case 'cdp_no_context':
        steps.push({
          key: ev.event,
          icon: ICON_WARN,
          text: 'Não consegui conectar ao Chrome para reconhecer a empresa.',
          detail: (ev.data.message as string | undefined) ?? ev.event
        })
        break
      case 'no_signal':
        steps.push({
          key: 'no_signal',
          icon: ICON_DOT,
          text: 'A aba People carregou, mas não vi a contagem de funcionários.'
        })
        break
      case 'error':
        steps.push({
          key: `error-${steps.length}`,
          icon: ICON_WARN,
          text: 'Erro inesperado',
          detail: ev.data.message as string | undefined
        })
        break
      default:
        // ignore unknown events
        break
    }
  }
  return steps
}
