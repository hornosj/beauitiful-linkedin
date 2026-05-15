import { describe, expect, it } from 'vitest'
import { delimiter, join } from 'node:path'
import { buildPythonCommands, buildSidecarEnv, DEFAULT_LOCAL_SEARXNG_URL } from '../src/main/sidecar'

describe('buildSidecarEnv', () => {
  it('adds project src to PYTHONPATH so the sidecar can run without editable install', () => {
    const projectRoot = 'C:\\project\\beautiful-linkedin'
    const env = buildSidecarEnv(projectRoot, {})

    expect(env.PYTHONPATH).toBe(join(projectRoot, 'src'))
    expect(env.PYTHONUNBUFFERED).toBe('1')
  })

  it('preserves an existing PYTHONPATH after project src', () => {
    const projectRoot = 'C:\\project\\beautiful-linkedin'
    const env = buildSidecarEnv(projectRoot, { PYTHONPATH: 'C:\\already-there' })

    expect(env.PYTHONPATH).toBe(`${join(projectRoot, 'src')}${delimiter}C:\\already-there`)
  })

  it('defaults SEARXNG_BASE_URL to the local docker mapping when not set', () => {
    const env = buildSidecarEnv(undefined, {})
    expect(env.SEARXNG_BASE_URL).toBe(DEFAULT_LOCAL_SEARXNG_URL)
  })

  it('does not override an existing SEARXNG_BASE_URL', () => {
    const env = buildSidecarEnv(undefined, { SEARXNG_BASE_URL: 'http://my-searx:9000' })
    expect(env.SEARXNG_BASE_URL).toBe('http://my-searx:9000')
  })

  it('treats whitespace-only SEARXNG_BASE_URL as missing and applies the default', () => {
    const env = buildSidecarEnv(undefined, { SEARXNG_BASE_URL: '   ' })
    expect(env.SEARXNG_BASE_URL).toBe(DEFAULT_LOCAL_SEARXNG_URL)
  })
})

describe('buildPythonCommands', () => {
  it('uses an explicit Python executable as the only command', () => {
    expect(buildPythonCommands('C:\\Python314\\python.exe')).toEqual([
      { executable: 'C:\\Python314\\python.exe', args: [] }
    ])
  })

  it('tries python and the Windows launcher when no executable is explicit', () => {
    expect(buildPythonCommands(undefined, 'win32')).toEqual([
      { executable: 'python', args: [] },
      { executable: 'py', args: ['-3'] }
    ])
  })
})
