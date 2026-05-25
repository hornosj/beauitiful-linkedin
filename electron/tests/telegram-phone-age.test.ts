import { describe, expect, it, vi } from 'vitest'

import {
  ageFromBirthDate,
  cpfAllowedForReview
} from '../src/renderer/src/components/CpfReviewList'

describe('ageFromBirthDate', () => {
  it('parses Brazilian dd/mm/yyyy and computes age relative to today', () => {
    vi.useFakeTimers()
    try {
      vi.setSystemTime(new Date('2026-05-22T12:00:00Z'))
      expect(ageFromBirthDate('15/03/1985')).toBe(41)
      // Pre-birthday: still 40 in early March 1985.
      expect(ageFromBirthDate('15/06/1985')).toBe(40)
    } finally {
      vi.useRealTimers()
    }
  })

  it('parses ISO yyyy-mm-dd', () => {
    vi.useFakeTimers()
    try {
      vi.setSystemTime(new Date('2026-05-22T12:00:00Z'))
      expect(ageFromBirthDate('1990-01-10')).toBe(36)
    } finally {
      vi.useRealTimers()
    }
  })

  it('handles 2-digit years using the 30 cutoff', () => {
    vi.useFakeTimers()
    try {
      vi.setSystemTime(new Date('2026-05-22T12:00:00Z'))
      // < 30 → 2000+
      expect(ageFromBirthDate('15/03/05')).toBe(21)
      // >= 30 → 1900+
      expect(ageFromBirthDate('15/03/70')).toBe(56)
    } finally {
      vi.useRealTimers()
    }
  })

  it('returns null for invalid or out-of-range values', () => {
    expect(ageFromBirthDate(null)).toBeNull()
    expect(ageFromBirthDate('')).toBeNull()
    expect(ageFromBirthDate('not a date')).toBeNull()
    expect(ageFromBirthDate('40/02/2000')).toBeNull()
    expect(ageFromBirthDate('15/13/2000')).toBeNull()
    expect(ageFromBirthDate('15/03/1800')).toBeNull()
  })

  it('does not allow CPF candidates older than 75 in the review list', () => {
    vi.useFakeTimers()
    try {
      vi.setSystemTime(new Date('2026-05-22T12:00:00Z'))
      expect(cpfAllowedForReview({ data_nascimento: '10/01/1950' })).toBe(false)
      expect(cpfAllowedForReview({ data_nascimento: '10/01/1951' })).toBe(true)
      expect(cpfAllowedForReview({ data_nascimento: null })).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })
})
