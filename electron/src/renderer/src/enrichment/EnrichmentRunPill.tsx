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
  const completed =
    run.meta.fields === 'phone'
      ? Math.min(run.phoneCompleted, total)
      : run.meta.fields === 'both'
        ? Math.min(Math.max(run.completed, run.phoneCompleted), total)
        : Math.min(run.completed, total)
  const ratio = total > 0 ? completed / total : 0
  const contactLabel =
    run.meta.fields === 'phone'
      ? 'telefones'
      : run.meta.fields === 'both'
        ? 'contatos'
        : 'e-mails'
  const doneCount =
    run.meta.fields === 'phone'
      ? run.summary?.enriched_phone_leads ?? 0
      : run.meta.fields === 'both'
        ? (run.summary?.enriched_leads ?? 0) + (run.summary?.enriched_phone_leads ?? 0)
        : run.summary?.enriched_leads ?? 0

  const label = done
    ? run.errorMessage
      ? 'Enriquecimento interrompido'
      : `${doneCount} novo(s) ${contactLabel}`
    : `Procurando ${contactLabel} · ${completed}/${total || '…'}`

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
