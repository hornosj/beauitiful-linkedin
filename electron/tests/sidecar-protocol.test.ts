import { describe, expect, it } from 'vitest'
import {
  parseBootStdout,
  PORT_TOKEN,
  READY_TOKEN
} from '../src/main/sidecar-protocol'

describe('parseBootStdout', () => {
  it('extracts port from BEAUTIFUL_LINKEDIN_PORT line', () => {
    const result = parseBootStdout('something\nBEAUTIFUL_LINKEDIN_PORT=39712\nmore noise')
    expect(result.port).toBe(39712)
  })

  it('detects ready token', () => {
    const result = parseBootStdout('BEAUTIFUL_LINKEDIN_PORT=39712\nBEAUTIFUL_LINKEDIN_READY')
    expect(result.ready).toBe(true)
  })

  it('returns null port when token absent', () => {
    expect(parseBootStdout('hello world').port).toBeNull()
  })

  it('exposes the protocol tokens for tests/integration', () => {
    expect(PORT_TOKEN).toBe('BEAUTIFUL_LINKEDIN_PORT')
    expect(READY_TOKEN).toBe('BEAUTIFUL_LINKEDIN_READY')
  })
})
