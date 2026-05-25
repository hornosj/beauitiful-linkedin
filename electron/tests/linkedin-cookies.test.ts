import { describe, expect, it } from 'vitest'
import {
  LINKEDIN_AUTH_COOKIE_NAMES,
  parseLinkedInCookieInput
} from '../src/main/linkedin-cookies'

describe('parseLinkedInCookieInput', () => {
  it('keeps a raw li_at value when the user pasted only the token', () => {
    expect(parseLinkedInCookieInput('AQEDabc123')).toEqual({
      liAt: 'AQEDabc123',
      jsessionid: null
    })
  })

  it('extracts li_at and JSESSIONID from a pasted Cookie header', () => {
    expect(
      parseLinkedInCookieInput(
        'Cookie: bcookie="v=2&abc"; li_at=AQEDabc123; JSESSIONID="ajax:123"; lang=v=2'
      )
    ).toEqual({
      liAt: 'AQEDabc123',
      jsessionid: '"ajax:123"'
    })
  })

  it('lists auth cookies that must be cleared before showing LinkedIn login', () => {
    expect(LINKEDIN_AUTH_COOKIE_NAMES).toEqual(['li_at', 'JSESSIONID'])
  })
})
