import type { FoundLead } from '../../../shared/types'

interface LiveFoundLeadsProps {
  leads: FoundLead[]
  running: boolean
}

/**
 * Progressive feedback panel for the search run. While the backend scrolls
 * the LinkedIn People tab, each lead it accepts is pushed here so the user
 * always sees the search making progress instead of staring at a spinner.
 *
 * Deliberately source-agnostic: it never names the provider or technique used
 * to find a lead — only the human-recognisable details.
 */
export default function LiveFoundLeads({ leads, running }: LiveFoundLeadsProps) {
  if (!running && leads.length === 0) {
    return null
  }

  return (
    <section className="live-found" aria-live="polite">
      <header className="live-found__header">
        <span className="live-found__pulse" aria-hidden={!running} />
        <strong>
          {running ? 'Encontrando leads…' : 'Leads encontrados'} ({leads.length})
        </strong>
        {running && (
          <span className="live-found__hint">
            Os resultados aparecem aqui conforme são descobertos.
          </span>
        )}
      </header>
      {leads.length === 0 ? (
        <p className="live-found__empty">Procurando pessoas que correspondem à sua busca…</p>
      ) : (
        <ul className="live-found__list">
          {leads.map((lead, index) => (
            <li className="live-found__item" key={lead.linkedin_url ?? `found-${index}`}>
              <div className="live-found__main">
                <span className="live-found__name">
                  {lead.person_name?.trim() || 'Pessoa sem nome'}
                </span>
                {lead.title && <span className="live-found__title">{lead.title}</span>}
              </div>
              <div className="live-found__meta">
                {lead.company_name && (
                  <span className="live-found__company">{lead.company_name}</span>
                )}
                {lead.location && <span className="live-found__loc">{lead.location}</span>}
                {lead.validation_status === 'maybe_incorrect' && (
                  <span className="live-found__tag live-found__tag--warn">
                    cargo a confirmar
                  </span>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
