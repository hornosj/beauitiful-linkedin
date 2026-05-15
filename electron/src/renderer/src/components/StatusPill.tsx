interface Props {
  baseUrl: string | null
  error?: string | null
}

export default function StatusPill({ baseUrl, error }: Props) {
  const ok = baseUrl !== null
  return (
    <div
      className="flex items-center gap-2 rounded-full border border-ink-200 bg-white px-3 py-1.5 dark:border-ink-700 dark:bg-ink-900"
      title={error ?? undefined}
    >
      <span
        className={`h-2 w-2 rounded-full ${ok ? 'bg-emerald-500' : 'bg-ink-300'} ${ok ? 'shadow-[0_0_0_3px_rgba(16,185,129,0.18)]' : ''}`}
      />
      <span className="text-xs font-medium text-ink-700 dark:text-ink-200">
        {ok ? `sidecar conectado` : 'sidecar offline'}
      </span>
      {ok && <span className="font-mono text-[11px] text-ink-500 dark:text-ink-400">{baseUrl}</span>}
    </div>
  )
}
