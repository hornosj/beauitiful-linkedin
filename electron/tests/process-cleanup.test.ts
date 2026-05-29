import { describe, expect, it, vi } from 'vitest'
import {
  cleanCompetingProcesses,
  parseListeningPidForPort,
  parseTasklistPids,
  type CleanRunDeps
} from '../src/main/process-cleanup'

describe('parseListeningPidForPort', () => {
  const netstat = [
    'Active Connections',
    '',
    '  Proto  Local Address          Foreign Address        State           PID',
    '  TCP    127.0.0.1:9223         0.0.0.0:0              LISTENING       24156',
    '  TCP    127.0.0.1:9222         0.0.0.0:0              LISTENING       1820',
    '  TCP    127.0.0.1:54074        0.0.0.0:0              LISTENING       8832',
    '  TCP    127.0.0.1:51000        127.0.0.1:9223         ESTABLISHED     9999'
  ].join('\r\n')

  it('finds the LISTENING pid for a port on its local address', () => {
    expect(parseListeningPidForPort(netstat, 9223)).toBe(24156)
    expect(parseListeningPidForPort(netstat, 9222)).toBe(1820)
  })

  it('ignores ports that only appear as a foreign address', () => {
    // 9223 also appears as a foreign address on the ESTABLISHED row (pid 9999) —
    // must return the LISTENING owner (24156), never the foreign-side pid.
    expect(parseListeningPidForPort(netstat, 9223)).toBe(24156)
    // 51000 only appears as a local ESTABLISHED address, never LISTENING.
    expect(parseListeningPidForPort(netstat, 51000)).toBeNull()
  })

  it('does not confuse a suffix port (9223 vs 19223)', () => {
    const withSuffix = '  TCP    127.0.0.1:19223        0.0.0.0:0              LISTENING       777'
    expect(parseListeningPidForPort(withSuffix, 9223)).toBeNull()
    expect(parseListeningPidForPort(withSuffix, 19223)).toBe(777)
  })

  it('returns null when nothing is listening', () => {
    expect(parseListeningPidForPort('no matches here', 9223)).toBeNull()
  })
})

describe('parseTasklistPids', () => {
  it('extracts pids from CSV tasklist output', () => {
    const csv = [
      '"beautiful-linkedin-sidecar.exe","24777","Console","1","120.000 K"',
      '"beautiful-linkedin-sidecar.exe","24999","Console","1","118.000 K"'
    ].join('\r\n')
    expect(parseTasklistPids(csv)).toEqual([24777, 24999])
  })

  it('returns empty for empty output', () => {
    expect(parseTasklistPids('')).toEqual([])
  })
})

describe('cleanCompetingProcesses', () => {
  it('kills the process holding each CDP port, except the current instance', async () => {
    const killTree = vi.fn().mockResolvedValue(undefined)
    const deps: CleanRunDeps = {
      listeningPid: vi.fn(async (port: number) => (port === 9222 ? 1820 : 4242)),
      killTree
    }

    // self owns 9223 (pid 4242) — must be preserved; 9222 owner (1820) killed.
    const report = await cleanCompetingProcesses(4242, null, deps)

    expect(killTree).toHaveBeenCalledTimes(1)
    expect(killTree).toHaveBeenCalledWith(1820)
    expect(report.killedPids).toEqual([1820])
    expect(report.ports).toEqual([
      { port: 9222, pid: 1820, killed: true },
      { port: 9223, pid: 4242, killed: false }
    ])
  })

  it('never kills the current pid or its sidecar', async () => {
    const killTree = vi.fn().mockResolvedValue(undefined)
    const deps: CleanRunDeps = {
      listeningPid: vi.fn(async (port: number) => (port === 9222 ? 555 : 999)),
      killTree,
      // Apenas o próprio sidecar (555) aparece — não deve ser varrido.
      listOrphanSidecarPids: vi.fn(async () => [555])
    }

    const report = await cleanCompetingProcesses(999, 555, deps)

    expect(killTree).not.toHaveBeenCalled()
    expect(report.killedPids).toEqual([])
  })

  it('does not kill the same pid twice when one process holds both ports', async () => {
    const killTree = vi.fn().mockResolvedValue(undefined)
    const deps: CleanRunDeps = {
      listeningPid: vi.fn(async () => 24156),
      killTree
    }

    const report = await cleanCompetingProcesses(1, null, deps)

    expect(killTree).toHaveBeenCalledTimes(1)
    expect(report.killedPids).toEqual([24156])
    expect(report.ports.map((p) => p.killed)).toEqual([true, false])
  })

  it('sweeps orphan packaged sidecars not owned by us', async () => {
    const killTree = vi.fn().mockResolvedValue(undefined)
    const deps: CleanRunDeps = {
      listeningPid: vi.fn(async () => null),
      killTree,
      listOrphanSidecarPids: vi.fn(async () => [30001, 30002])
    }

    const report = await cleanCompetingProcesses(1, 30002, deps)

    expect(killTree).toHaveBeenCalledTimes(1)
    expect(killTree).toHaveBeenCalledWith(30001)
    expect(report.killedPids).toEqual([30001])
  })

  it('records errors without aborting the whole clean run', async () => {
    const deps: CleanRunDeps = {
      listeningPid: vi.fn(async (port: number) => (port === 9222 ? 1820 : null)),
      killTree: vi.fn(async () => {
        throw new Error('Access denied')
      })
    }

    const report = await cleanCompetingProcesses(1, null, deps)

    expect(report.killedPids).toEqual([])
    expect(report.errors).toHaveLength(1)
    expect(report.errors[0]).toContain('1820')
    expect(report.ports[0]).toEqual({ port: 9222, pid: 1820, killed: false })
  })
})
