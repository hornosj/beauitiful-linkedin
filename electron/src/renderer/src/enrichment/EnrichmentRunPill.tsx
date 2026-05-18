import { useEnrichmentRunner } from './EnrichmentRunnerContext'

/**
 * Floating chip that stays visible whenever an enrichment run exists but
 * the full modal is closed. Click to re-open the modal; the ✕ dismisses
 * the chip (cancels if still running, clears if already done).
 */
export default function EnrichmentRunPill() {
  const { run, modalOpen, openModal, dismiss } = useEnrichmentRunner()
  if (!run || modalOpen) return null

  const done = !run.running
  const total = run.totalLeads || 0
  const completed = Math.min(run.completed, total)
  const ratio = total > 0 ? completed / total : 0

  const label = done
    ? run.errorMessage
      ? 'Enriquecimento interrompido'
      : `${run.summary?.enriched_leads ?? 0} novo(s) e-mail(s)`
    : `Procurando e-mails · ${completed}/${total || '…'}`

  return (
    <button
      type="button"
      className="enrich-pill"
      data-state={done ? 'done' : 'running'}
      onClick={openModal}
      aria-label="Reabrir progresso do enriquecimento"
    >
      <span className="enrich-pill-dot" aria-hidden="true" />
      <span className="enrich-pill-body">
        <span className="enrich-pill-table">{run.meta.tableName}</span>
        <span className="enrich-pill-status">{label}</span>
      </span>
      <span
        className="enrich-pill-track"
        aria-hidden="true"
        style={{ '--enrich-pill-progress': ratio } as React.CSSProperties}
      />
      <span
        className="enrich-pill-close"
        role="button"
        tabIndex={-1}
        aria-label="Dispensar"
        onClick={(event) => {
          event.stopPropagation()
          dismiss()
        }}
      >
        ✕
      </span>
    </button>
  )
}
