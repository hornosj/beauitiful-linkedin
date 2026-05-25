export interface ParsedLinkedInCookies {
  liAt: string | null
  jsessionid: string | null
}

export const LINKEDIN_AUTH_COOKIE_NAMES = ['li_at', 'JSESSIONID'] as const

export function parseLinkedInCookieInput(input: string | null | undefined): ParsedLinkedInCookies {
  const cleaned = (input ?? '').trim()
  if (!cleaned) return { liAt: null, jsessionid: null }

  const header = cleaned.replace(/^cookie:\s*/i, '')
  if (!header.includes('=')) {
    return { liAt: header, jsessionid: null }
  }

  let liAt: string | null = null
  let jsessionid: string | null = null
  for (const part of header.split(';')) {
    const segment = part.trim()
    if (!segment) continue
    const eq = segment.indexOf('=')
    if (eq <= 0) continue
    const name = segment.slice(0, eq).trim().toLowerCase()
    const value = segment.slice(eq + 1).trim()
    if (!value) continue
    if (name === 'li_at') liAt = value
    if (name === 'jsessionid') jsessionid = value
  }

  return { liAt, jsessionid }
}
