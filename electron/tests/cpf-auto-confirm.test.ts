import { describe, expect, it } from 'vitest'
import { shouldAutoConfirmCpf } from '../src/renderer/src/components/SavedLeadsLibrary'

describe('shouldAutoConfirmCpf', () => {
  it('auto-confirms a dominant high-score candidate', () => {
    expect(
      shouldAutoConfirmCpf([
        { cpf: '111', match_score: 92 },
        { cpf: '222', match_score: 60 }
      ])
    ).toBe('111')
  })

  it('auto-confirms a lone high-score candidate', () => {
    expect(shouldAutoConfirmCpf([{ cpf: '111', match_score: 88 }])).toBe('111')
  })

  it('returns null when the top score is below the auto-confirm threshold', () => {
    expect(shouldAutoConfirmCpf([{ cpf: '111', match_score: 80 }])).toBeNull()
  })

  it('returns null when the top is not dominant over the second (ambiguous)', () => {
    expect(
      shouldAutoConfirmCpf([
        { cpf: '111', match_score: 90 },
        { cpf: '222', match_score: 85 }
      ])
    ).toBeNull()
  })

  it('returns null for an empty candidate list', () => {
    expect(shouldAutoConfirmCpf([])).toBeNull()
  })
})
