import { describe, expect, it } from 'vitest'

import { signalMatched } from '../src/renderer/src/components/CpfReviewList'

describe('signalMatched', () => {
  it('returns true when the breakdown entry has earned > 0', () => {
    expect(
      signalMatched('location', {
        location: { score: 80, earned: 24, weight: 30 }
      })
    ).toBe(true)
  })

  it('returns false when earned is zero (signal was compared but missed)', () => {
    expect(
      signalMatched('location', {
        location: { score: 0, earned: 0, weight: 30 }
      })
    ).toBe(false)
  })

  it('falls back to score when earned is absent (older payloads)', () => {
    expect(signalMatched('name', { name: { score: 95 } })).toBe(true)
    expect(signalMatched('name', { name: { score: 0 } })).toBe(false)
  })

  it('treats name_gate passed=true as matched', () => {
    expect(
      signalMatched('name_gate', { name_gate: { passed: true, reason: 'ok' } })
    ).toBe(true)
    expect(
      signalMatched('name_gate', { name_gate: { passed: false, reason: 'mismatch' } })
    ).toBe(false)
  })

  it('returns false for missing entries and bad input', () => {
    expect(signalMatched('location', {})).toBe(false)
    expect(signalMatched('location', undefined)).toBe(false)
    expect(signalMatched('location', null)).toBe(false)
    expect(signalMatched('location', { location: 'not-an-object' })).toBe(false)
    expect(signalMatched('location', { location: { score: null, earned: null } })).toBe(false)
  })
})
