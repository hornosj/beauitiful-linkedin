import { describe, expect, it } from 'vitest'
import { delimiter, join } from 'node:path'
import {
  buildPythonCommands,
  buildSidecarCommands,
  buildSidecarEnv,
  DEFAULT_LOCAL_SEARXNG_URL,
  resolvePackagedSidecarExecutable
} from '../src/main/sidecar'

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

describe('buildSidecarCommands', () => {
  it('runs the packaged sidecar executable when provided', () => {
    expect(buildSidecarCommands({
      packagedSidecarExecutable: 'C:\\app\\resources\\sidecar\\beautiful-linkedin-sidecar.exe'
    })).toEqual([
      {
        executable: 'C:\\app\\resources\\sidecar\\beautiful-linkedin-sidecar.exe',
        args: []
      }
    ])
  })

  it('runs the Python module in development', () => {
    expect(buildSidecarCommands({ platform: 'linux' })).toEqual([
      {
        executable: 'python',
        args: ['-m', 'beautiful_linkedin.server']
      }
    ])
  })

  it('uses an explicit Python executable for development commands', () => {
    expect(buildSidecarCommands({
      explicitPythonExecutable: 'C:\\Python314\\python.exe',
      platform: 'win32'
    })).toEqual([
      {
        executable: 'C:\\Python314\\python.exe',
        args: ['-m', 'beautiful_linkedin.server']
      }
    ])
  })
})

describe('resolvePackagedSidecarExecutable', () => {
  it('returns undefined outside packaged builds', () => {
    expect(resolvePackagedSidecarExecutable(false, '/Applications/App.app/Contents/Resources', 'darwin')).toBeUndefined()
  })

  it('resolves the Windows sidecar executable inside resources', () => {
    expect(resolvePackagedSidecarExecutable(true, 'C:\\app\\resources', 'win32')).toBe(
      'C:\\app\\resources\\sidecar\\beautiful-linkedin-sidecar.exe'
    )
  })

  it('resolves the macOS sidecar executable inside resources', () => {
    expect(resolvePackagedSidecarExecutable(true, '/Applications/Beautiful LinkedIn.app/Contents/Resources', 'darwin')).toBe(
      '/Applications/Beautiful LinkedIn.app/Contents/Resources/sidecar/beautiful-linkedin-sidecar'
    )
  })
})
