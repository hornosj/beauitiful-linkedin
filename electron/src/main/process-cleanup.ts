import { exec } from 'node:child_process'
import { promisify } from 'node:util'

const execAsync = promisify(exec)

// Portas de debug CDP fixas usadas pelo app: 9222 = Chrome externo lançado por
// launchChromeWithCdp; 9223 = Chromium embutido (remote-debugging-port em
// index.ts). Quando uma instância anterior do app não encerra direito, ela
// continua segurando essas portas e o cache de userData. A instância nova falha
// ao fazer bind ("Cannot start http server for devtools") e o sidecar acaba
// falando com o Chromium errado — busca trava em 0 leads. O clean run mata quem
// segura essas portas (fora a própria instância) e reinicia limpo.
export const CDP_PORTS = [9222, 9223] as const

// Nome do executável do sidecar empacotado. É exclusivo deste app, então é
// seguro varrer por imagem; em dev o sidecar é `python -m ...` (image python.exe)
// e NÃO é varrido por nome para não matar outros processos Python do usuário.
const PACKAGED_SIDECAR_IMAGE = 'beautiful-linkedin-sidecar.exe'

export interface CleanRunPortResult {
  port: number
  pid: number | null
  killed: boolean
}

export interface CleanRunReport {
  killedPids: number[]
  ports: CleanRunPortResult[]
  errors: string[]
}

export interface CleanRunDeps {
  /** PID que está em LISTEN na porta, ou null se ninguém. */
  listeningPid(port: number): Promise<number | null>
  /** Mata o processo e toda a sua árvore de filhos. */
  killTree(pid: number): Promise<void>
  /** PIDs de sidecars empacotados órfãos (best-effort, opcional). */
  listOrphanSidecarPids?(): Promise<number[]>
}

/**
 * Extrai o PID em estado LISTENING para uma porta a partir da saída de
 * `netstat -ano -p tcp` no Windows. Olha apenas a coluna de endereço local.
 */
export function parseListeningPidForPort(netstatStdout: string, port: number): number | null {
  for (const line of netstatStdout.split(/\r?\n/)) {
    const trimmed = line.trim()
    if (!/\bLISTENING\b/i.test(trimmed)) continue
    const cols = trimmed.split(/\s+/)
    if (cols.length < 5) continue
    const local = cols[1]
    if (!local.endsWith(`:${port}`)) continue
    const pid = Number(cols[cols.length - 1])
    if (Number.isInteger(pid) && pid > 0) return pid
  }
  return null
}

/** Extrai PIDs da saída CSV de `tasklist ... /FO CSV /NH`. */
export function parseTasklistPids(csvStdout: string): number[] {
  const pids: number[] = []
  for (const line of csvStdout.split(/\r?\n/)) {
    const fields = line.split('","').map((f) => f.replace(/^"|"$/g, ''))
    if (fields.length < 2) continue
    const pid = Number(fields[1])
    if (Number.isInteger(pid) && pid > 0) pids.push(pid)
  }
  return pids
}

async function defaultListeningPid(port: number, platform: NodeJS.Platform): Promise<number | null> {
  if (platform === 'win32') {
    try {
      const { stdout } = await execAsync('netstat -ano -p tcp')
      return parseListeningPidForPort(stdout, port)
    } catch {
      return null
    }
  }
  try {
    const { stdout } = await execAsync(`lsof -nP -iTCP:${port} -sTCP:LISTEN -t`)
    const pid = Number(stdout.trim().split(/\s+/)[0])
    return Number.isInteger(pid) && pid > 0 ? pid : null
  } catch {
    return null
  }
}

async function defaultKillTree(pid: number, platform: NodeJS.Platform): Promise<void> {
  if (platform === 'win32') {
    await execAsync(`taskkill /F /T /PID ${pid}`)
    return
  }
  await execAsync(`pkill -TERM -P ${pid}`).catch(() => {})
  try {
    process.kill(pid, 'SIGKILL')
  } catch {
    // já morreu
  }
}

async function defaultListOrphanSidecarPids(platform: NodeJS.Platform): Promise<number[]> {
  if (platform !== 'win32') return []
  try {
    const { stdout } = await execAsync(
      `tasklist /FI "IMAGENAME eq ${PACKAGED_SIDECAR_IMAGE}" /FO CSV /NH`
    )
    return parseTasklistPids(stdout)
  } catch {
    return []
  }
}

export function createDefaultCleanRunDeps(
  platform: NodeJS.Platform = process.platform
): CleanRunDeps {
  return {
    listeningPid: (port) => defaultListeningPid(port, platform),
    killTree: (pid) => defaultKillTree(pid, platform),
    listOrphanSidecarPids: () => defaultListOrphanSidecarPids(platform)
  }
}

/**
 * Mata processos concorrentes que seguram as portas de debug CDP e sidecars
 * órfãos, preservando a instância atual e seu sidecar (que serão reiniciados
 * pelo chamador). Retorna um relatório do que foi encerrado.
 */
export async function cleanCompetingProcesses(
  selfPid: number,
  sidecarPid: number | null,
  deps: CleanRunDeps = createDefaultCleanRunDeps()
): Promise<CleanRunReport> {
  const report: CleanRunReport = { killedPids: [], ports: [], errors: [] }
  const protectedPids = new Set<number>([selfPid])
  if (sidecarPid && sidecarPid > 0) protectedPids.add(sidecarPid)

  for (const port of CDP_PORTS) {
    let pid: number | null = null
    try {
      pid = await deps.listeningPid(port)
    } catch (error) {
      report.errors.push(`porta ${port}: ${errMsg(error)}`)
    }

    let killed = false
    if (pid && !protectedPids.has(pid) && !report.killedPids.includes(pid)) {
      try {
        await deps.killTree(pid)
        killed = true
        report.killedPids.push(pid)
      } catch (error) {
        report.errors.push(`encerrar PID ${pid} (porta ${port}): ${errMsg(error)}`)
      }
    }
    report.ports.push({ port, pid, killed })
  }

  if (deps.listOrphanSidecarPids) {
    let orphans: number[] = []
    try {
      orphans = await deps.listOrphanSidecarPids()
    } catch (error) {
      report.errors.push(`listar sidecars órfãos: ${errMsg(error)}`)
    }
    for (const pid of orphans) {
      if (protectedPids.has(pid) || report.killedPids.includes(pid)) continue
      try {
        await deps.killTree(pid)
        report.killedPids.push(pid)
      } catch (error) {
        report.errors.push(`encerrar sidecar órfão ${pid}: ${errMsg(error)}`)
      }
    }
  }

  return report
}

function errMsg(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}
