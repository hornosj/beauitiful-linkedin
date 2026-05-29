import { useState } from 'react'

import type { TelegramPhoneRankedCandidate } from '../../../shared/types'

/**
 * Lista de revisão de CPFs candidatos. Renderiza cada candidato com nome
 * (grande), CPF (mono), endereço, sinais usados pelo matcher, idade e
 * badge de score. O operador marca quais CPFs ele quer consultar; o
 * caller decide o que fazer com a lista (no app, sempre dispara o estágio
 * de telefone via Telethon).
 *
 * Este componente era originalmente parte de `TelegramPhoneRunDialog`,
 * mas foi extraído pra própria casa porque o fluxo legado (Playwright +
 * Chrome) foi removido — agora só o `CpfPickerTelethonDialog` consome.
 */
export function CpfReviewList({
  candidates,
  defaultSelected,
  leadName,
  leadRef,
  onConfirm,
  onSkip,
  busy
}: {
  candidates: TelegramPhoneRankedCandidate[]
  defaultSelected: string[]
  leadName: string | null
  leadRef: string
  onConfirm(cpfs: string[]): Promise<void> | void
  onSkip(): Promise<void> | void
  busy: boolean
}): JSX.Element {
  const candidateList = Array.isArray(candidates) ? candidates.filter(cpfAllowedForReview) : []
  const reviewCpfSet = new Set(candidateList.map((c) => c.cpf))
  const [selected, setSelected] = useState<Set<string>>(
    () =>
      new Set(
        Array.isArray(defaultSelected)
          ? defaultSelected.filter((cpf) => reviewCpfSet.has(cpf))
          : []
      )
  )

  const toggle = (cpf: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(cpf)) next.delete(cpf)
      else next.add(cpf)
      return next
    })
  }

  const submit = () => {
    const chosen = Array.from(selected).filter((cpf) => reviewCpfSet.has(cpf))
    if (chosen.length === 0) {
      void onSkip()
      return
    }
    void onConfirm(chosen)
  }

  const linkedInUrl = isLinkedInUrl(leadRef) ? leadRef : null
  const displayName = leadName ?? (linkedInUrl ? '(lead sem nome)' : leadRef)

  return (
    <div className="enrich-progress-stack" style={{ gap: 12 }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 12,
          padding: '10px 12px',
          borderRadius: 9,
          background: 'var(--surface-2, rgba(0,0,0,0.03))',
          border: '0.5px solid var(--line)'
        }}
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
          <span
            style={{
              fontSize: 11,
              color: 'var(--ink-3)',
              textTransform: 'uppercase',
              letterSpacing: 0.4
            }}
          >
            Lead em revisão
          </span>
          <strong style={{ color: 'var(--ink)', fontSize: 14, fontWeight: 600 }}>
            {displayName}
          </strong>
        </div>
        {linkedInUrl ? (
          <a
            href={linkedInUrl}
            target="_blank"
            rel="noreferrer"
            className="enrich-btn ghost"
            style={{
              textDecoration: 'none',
              fontSize: 12,
              padding: '6px 12px',
              whiteSpace: 'nowrap',
              display: 'inline-flex',
              alignItems: 'center',
              gap: 6
            }}
            title="Abrir perfil do LinkedIn em outra aba para conferir foto/idade do lead"
          >
            <span aria-hidden="true">🔗</span> Abrir LinkedIn
          </a>
        ) : (
          <span style={{ fontSize: 11, color: 'var(--ink-4)' }}>sem URL do LinkedIn</span>
        )}
      </div>
      <p style={{ fontSize: 12, color: 'var(--ink-3)', margin: 0 }}>
        Bata o olho no LinkedIn para estimar a idade — depois marque o CPF cuja idade
        bate com a pessoa.
      </p>
      <ul
        className="enrich-lead-feed"
        aria-label="CPFs candidatos"
        style={{ display: 'grid', gap: 8 }}
      >
        {candidateList.map((c) => {
          const isChecked = selected.has(c.cpf)
          const confidence = confidenceLabel(c.match_score)
          const eligible = c.eligible
          const rejectionReason = (c.breakdown as { rejection_reason?: unknown } | undefined)
            ?.rejection_reason
          const age = ageFromBirthDate(c.data_nascimento ?? null)
          return (
            <li
              key={c.cpf}
              data-status={eligible ? 'enriched' : 'no_change'}
              style={{
                display: 'flex',
                alignItems: 'stretch',
                gap: 0,
                padding: 0,
                borderRadius: 10,
                border: '0.5px solid var(--line)',
                background: eligible
                  ? 'rgba(52, 199, 89, 0.08)'
                  : 'var(--surface-2, transparent)',
                borderColor: eligible ? 'rgba(52, 199, 89, 0.22)' : 'var(--line)',
                opacity: eligible ? 1 : 0.85,
                fontSize: 12.5,
                color: 'var(--ink-2)',
                overflow: 'hidden'
              }}
            >
              <label
                style={{
                  display: 'flex',
                  alignItems: 'stretch',
                  gap: 12,
                  width: '100%',
                  cursor: 'pointer',
                  minWidth: 0,
                  padding: '12px 14px'
                }}
              >
                <input
                  type="checkbox"
                  checked={isChecked}
                  onChange={() => toggle(c.cpf)}
                  disabled={busy}
                  style={{
                    marginTop: 4,
                    flexShrink: 0,
                    width: 16,
                    height: 16,
                    cursor: 'pointer'
                  }}
                />
                <div
                  style={{
                    display: 'flex',
                    flexDirection: 'column',
                    gap: 4,
                    flex: 1,
                    minWidth: 0
                  }}
                >
                  <strong
                    style={{
                      color: 'var(--ink)',
                      fontSize: 14,
                      fontWeight: 600,
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap'
                    }}
                    title={c.nome ?? undefined}
                  >
                    {c.nome ?? '(sem nome no retorno do bot)'}
                  </strong>
                  <code
                    style={{
                      fontFamily: 'var(--font-mono)',
                      fontSize: 12,
                      background: 'var(--surface-3, rgba(0,0,0,0.04))',
                      padding: '2px 6px',
                      borderRadius: 4,
                      color: 'var(--ink)',
                      alignSelf: 'flex-start'
                    }}
                  >
                    {c.cpf}
                  </code>
                  {c.endereco && (
                    <span
                      style={{
                        fontSize: 11.5,
                        color: 'var(--ink-3)',
                        marginTop: 2,
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap'
                      }}
                    >
                      📍 {truncate(c.endereco, 80)}
                    </span>
                  )}
                  {(c.signals_used?.length ?? 0) > 0 && (
                    <SignalChips signals={c.signals_used} breakdown={c.breakdown} />
                  )}
                  {!eligible && (
                    <span style={{ color: '#c97400', fontSize: 11.5, marginTop: 2 }}>
                      ⚠ Rejeitado pelo matcher
                      {rejectionReason ? ` (${String(rejectionReason)})` : ''}
                    </span>
                  )}
                </div>
                <div
                  style={{
                    display: 'flex',
                    flexDirection: 'column',
                    alignItems: 'flex-end',
                    justifyContent: 'space-between',
                    gap: 6,
                    flexShrink: 0,
                    minWidth: 92
                  }}
                >
                  <div
                    style={{
                      display: 'flex',
                      flexDirection: 'column',
                      alignItems: 'flex-end',
                      gap: 0
                    }}
                  >
                    {age !== null ? (
                      <>
                        <span
                          style={{
                            fontSize: 22,
                            fontWeight: 700,
                            color: 'var(--ink)',
                            lineHeight: 1,
                            fontVariantNumeric: 'tabular-nums'
                          }}
                        >
                          {age}
                        </span>
                        <span style={{ fontSize: 10, color: 'var(--ink-4)', marginTop: 2 }}>
                          anos
                        </span>
                      </>
                    ) : c.data_nascimento ? (
                      <span style={{ fontSize: 11, color: 'var(--ink-4)' }}>
                        Nasc.: {c.data_nascimento}
                      </span>
                    ) : (
                      <span style={{ fontSize: 11, color: 'var(--ink-4)' }}>idade ?</span>
                    )}
                  </div>
                  <ScoreBadge score={c.match_score} confidence={confidence} />
                </div>
              </label>
            </li>
          )
        })}
      </ul>

      <div
        style={{
          display: 'flex',
          gap: 8,
          justifyContent: 'flex-end',
          paddingTop: 8,
          borderTop: '1px solid var(--ink-5, rgba(0,0,0,0.08))'
        }}
      >
        <button
          type="button"
          className="enrich-btn ghost"
          onClick={() => {
            void onSkip()
          }}
          disabled={busy}
        >
          Pular este lead
        </button>
        <button
          type="button"
          className="enrich-btn primary"
          onClick={submit}
          disabled={busy}
          autoFocus
        >
          {busy
            ? 'Consultando…'
            : selected.size === 0
              ? 'Pular este lead'
              : `Buscar telefones (${selected.size} CPF${selected.size === 1 ? '' : 's'})`}
        </button>
      </div>
    </div>
  )
}

const SIGNAL_LABELS: Record<string, string> = {
  name: 'Nome',
  location: 'Localização',
  career_age: 'Idade (carreira)',
  education_age: 'Idade (formação)',
  age: 'Idade',
  data_quality: 'Qualidade',
  name_gate: 'Gate nome'
}

function signalLabel(signal: string): string {
  return SIGNAL_LABELS[signal] ?? signal
}

/**
 * Um sinal está "matched" quando a entrada correspondente do breakdown
 * tem ``earned > 0`` (o matcher de fato somou pontos por aquele sinal).
 * Caso a entrada esteja presente mas ``earned`` seja zero/nulo, o sinal
 * foi comparado mas não bateu — vai em cinza. `name_gate` carrega só
 * `passed` — `passed=true` conta como matched.
 */
export function signalMatched(
  signal: string,
  breakdown: Record<string, unknown> | undefined | null
): boolean {
  if (!breakdown) return false
  const entry = (breakdown as Record<string, unknown>)[signal]
  if (!entry || typeof entry !== 'object') return false
  const earned = (entry as { earned?: unknown }).earned
  if (typeof earned === 'number' && earned > 0) return true
  const score = (entry as { score?: unknown }).score
  if (typeof score === 'number' && score > 0) return true
  const passed = (entry as { passed?: unknown }).passed
  if (passed === true) return true
  return false
}

function SignalChips({
  signals,
  breakdown
}: {
  signals: string[]
  breakdown: Record<string, unknown> | undefined
}): JSX.Element {
  const seen = new Set<string>()
  const items = signals.filter((s) => {
    if (seen.has(s)) return false
    seen.add(s)
    return true
  })
  return (
    <div
      style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginTop: 4 }}
      aria-label="Sinais usados pelo matcher"
    >
      {items.map((signal) => {
        const matched = signalMatched(signal, breakdown)
        const isLocation = signal === 'location'
        return (
          <span
            key={signal}
            data-matched={matched ? 'true' : 'false'}
            title={
              matched
                ? `Sinal "${signalLabel(signal)}" bateu com o lead`
                : `Sinal "${signalLabel(signal)}" foi comparado mas não bateu`
            }
            style={{
              fontSize: 10.5,
              padding: '2px 8px',
              borderRadius: 999,
              fontWeight: 600,
              letterSpacing: 0.2,
              color: matched ? '#1f7a3a' : 'var(--ink-3, #666)',
              background: matched ? 'rgba(52, 199, 89, 0.16)' : 'rgba(0, 0, 0, 0.04)',
              border: matched
                ? '0.5px solid rgba(52, 199, 89, 0.32)'
                : '0.5px solid var(--line, rgba(0,0,0,0.08))',
              whiteSpace: 'nowrap'
            }}
          >
            {matched ? '✓ ' : ''}
            {signalLabel(signal)}
            {isLocation && matched ? ' (LinkedIn)' : ''}
          </span>
        )
      })}
    </div>
  )
}

function ScoreBadge({
  score,
  confidence
}: {
  score: number
  confidence: string
}): JSX.Element {
  const color =
    score >= 85 ? '#1f7a3a' : score >= 65 ? '#a16207' : score >= 40 ? '#c2410c' : '#b91c1c'
  const bg =
    score >= 85
      ? 'rgba(52, 199, 89, 0.14)'
      : score >= 65
        ? 'rgba(245, 158, 11, 0.14)'
        : score >= 40
          ? 'rgba(234, 88, 12, 0.14)'
          : 'rgba(220, 38, 38, 0.12)'
  return (
    <span
      style={{
        fontSize: 11,
        fontWeight: 600,
        padding: '2px 8px',
        borderRadius: 999,
        color,
        background: bg,
        fontVariantNumeric: 'tabular-nums',
        whiteSpace: 'nowrap'
      }}
      title={`Score do matcher (0-100). Confiança: ${confidence}.`}
    >
      Score {score} · {confidence}
    </span>
  )
}

function confidenceLabel(score: number): string {
  if (score >= 85) return 'alta'
  if (score >= 65) return 'média'
  if (score >= 40) return 'fraca'
  return 'baixa'
}

function truncate(value: string, max: number): string {
  if (value.length <= max) return value
  return value.slice(0, max - 1) + '…'
}

function isLinkedInUrl(value: string | null | undefined): boolean {
  if (!value) return false
  return /^https?:\/\/([a-z]+\.)?linkedin\.com\//i.test(value)
}

/**
 * Parseia datas no formato brasileiro retornadas pelo Gonzales:
 * `dd/mm/yyyy` ou `dd/mm/yy` (com 2 dígitos, < 30 vira 2000+; >= 30 vira
 * 1900+). Aceita também `yyyy-mm-dd`. Retorna a idade em anos completos
 * hoje, ou null se a data não bater nada plausível.
 */
export function ageFromBirthDate(value: string | null): number | null {
  if (!value) return null
  const trimmed = value.trim()
  if (!trimmed) return null
  let year: number | null = null
  let month: number | null = null
  let day: number | null = null
  const br = /^(\d{1,2})\/(\d{1,2})\/(\d{2,4})$/.exec(trimmed)
  if (br) {
    day = Number(br[1])
    month = Number(br[2])
    year = Number(br[3])
    if (year < 100) {
      year = year < 30 ? 2000 + year : 1900 + year
    }
  } else {
    const iso = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(trimmed)
    if (iso) {
      year = Number(iso[1])
      month = Number(iso[2])
      day = Number(iso[3])
    }
  }
  if (!year || !month || !day) return null
  if (year < 1900 || year > 2100) return null
  if (month < 1 || month > 12) return null
  if (day < 1 || day > 31) return null
  const today = new Date()
  let age = today.getFullYear() - year
  const monthDiff = today.getMonth() + 1 - month
  if (monthDiff < 0 || (monthDiff === 0 && today.getDate() < day)) {
    age -= 1
  }
  if (age < 0 || age > 130) return null
  return age
}

export function cpfAllowedForReview(candidate: {
  data_nascimento?: string | null
}): boolean {
  const age = ageFromBirthDate(candidate.data_nascimento ?? null)
  return age !== null && age <= 75
}
