import { spawn, type ChildProcess } from 'node:child_process'
import { once } from 'node:events'
import { delimiter, join, posix, win32 } from 'node:path'
import { parseBootStdout, READY_TOKEN } from './sidecar-protocol'

export interface SidecarHandle {
  port: number
  baseUrl: string
  process: ChildProcess
  shutdown(): Promise<void>
}

export interface StartSidecarOptions {
  pythonExecutable?: string
  sidecarExecutablePath?: string
  projectRoot?: string
  cwd?: string
  env?: NodeJS.ProcessEnv
  startTimeoutMs?: number
  onLog?: (line: string) => void
}

const DEFAULT_TIMEOUT_MS = 20_000

interface PythonCommand {
  executable: string
  args: string[]
}

interface SidecarCommand {
  executable: string
  args: string[]
}

export function buildPythonCommands(
  explicitExecutable?: string,
  platform: NodeJS.Platform = process.platform
): PythonCommand[] {
  if (explicitExecutable) return [{ executable: explicitExecutable, args: [] }]
  const commands: PythonCommand[] = [{ executable: 'python', args: [] }]
  if (platform === 'win32') commands.push({ executable: 'py', args: ['-3'] })
  return commands
}

export function buildSidecarCommands(options: {
  explicitPythonExecutable?: string
  packagedSidecarExecutable?: string
  platform?: NodeJS.Platform
} = {}): SidecarCommand[] {
  if (options.packagedSidecarExecutable) {
    return [{ executable: options.packagedSidecarExecutable, args: [] }]
  }

  return buildPythonCommands(options.explicitPythonExecutable, options.platform).map((command) => ({
    executable: command.executable,
    args: [...command.args, '-m', 'beautiful_linkedin.server']
  }))
}

export function resolvePackagedSidecarExecutable(
  isPackaged: boolean,
  resourcesPath: string,
  platform: NodeJS.Platform = process.platform
): string | undefined {
  if (!isPackaged) return undefined
  const executableName =
    platform === 'win32'
      ? 'beautiful-linkedin-sidecar.exe'
      : 'beautiful-linkedin-sidecar'
  const pathApi = platform === 'win32' ? win32 : posix
  return pathApi.join(resourcesPath, 'sidecar', executableName)
}

export const DEFAULT_LOCAL_SEARXNG_URL = 'http://127.0.0.1:8080'

export function buildSidecarEnv(projectRoot: string | undefined, env: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  const srcPath = projectRoot ? join(projectRoot, 'src') : undefined
  const pythonPath = [srcPath, env.PYTHONPATH].filter(Boolean).join(delimiter)
  // Default the local SearxNG endpoint to the docker-compose mapping when the
  // user hasn't explicitly configured one. Without this the sidecar would
  // ignore SearxNG entirely and fall back to direct DDG/Bing/Google scrapers,
  // which are essentially always rate-limited from a single household IP.
  const existingSearxng = (env.SEARXNG_BASE_URL ?? '').trim()
  return {
    ...env,
    ...(pythonPath ? { PYTHONPATH: pythonPath } : {}),
    ...(existingSearxng ? {} : { SEARXNG_BASE_URL: DEFAULT_LOCAL_SEARXNG_URL }),
    PYTHONUNBUFFERED: '1',
    PYTHONIOENCODING: 'utf-8',
    PYTHONUTF8: '1'
  }
}

export async function startSidecar(options: StartSidecarOptions = {}): Promise<SidecarHandle> {
  const commands = buildSidecarCommands({
    explicitPythonExecutable: options.pythonExecutable ?? process.env.BEAUTIFUL_LINKEDIN_PYTHON,
    packagedSidecarExecutable: options.sidecarExecutablePath
  })
  const errors: string[] = []
  for (const command of commands) {
    try {
      return await startSidecarWithCommand(command, options)
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      errors.push(`${command.executable} ${command.args.join(' ')}: ${message}`.trim())
      options.onLog?.(`[sidecar] Falha com ${command.executable}: ${message}`)
    }
  }
  throw new Error(`Não foi possível iniciar o sidecar Python. ${errors.join(' | ')}`)
}

async function startSidecarWithCommand(
  command: SidecarCommand,
  options: StartSidecarOptions
): Promise<SidecarHandle> {
  const child = spawn(command.executable, command.args, {
    cwd: options.cwd ?? options.projectRoot,
    env: buildSidecarEnv(options.projectRoot, { ...process.env, ...(options.env ?? {}) }),
    stdio: ['ignore', 'pipe', 'pipe']
  })

  let buffer = ''
  let resolved = false
  let port: number | null = null
  const onLog = options.onLog ?? (() => {})

  child.stdout?.setEncoding('utf-8')
  child.stderr?.setEncoding('utf-8')
  child.stderr?.on('data', (chunk: string) => onLog(`[sidecar:err] ${chunk.trim()}`))

  const ready = new Promise<number>((resolve, reject) => {
    const timeout = setTimeout(() => {
      if (!resolved) {
        resolved = true
        reject(new Error('Timeout aguardando o sidecar Python iniciar.'))
      }
    }, options.startTimeoutMs ?? DEFAULT_TIMEOUT_MS)

    child.stdout?.on('data', (chunk: string) => {
      buffer += chunk
      onLog(`[sidecar] ${chunk.trim()}`)
      const snapshot = parseBootStdout(buffer)
      if (snapshot.port !== null) port = snapshot.port
      if (port !== null && snapshot.ready && !resolved) {
        resolved = true
        clearTimeout(timeout)
        resolve(port)
      }
    })

    child.once('exit', (code) => {
      if (!resolved) {
        resolved = true
        clearTimeout(timeout)
        reject(new Error(`Sidecar saiu antes de ficar pronto (código=${code}). Veja os logs acima.`))
      }
    })

    child.once('error', (error) => {
      if (!resolved) {
        resolved = true
        clearTimeout(timeout)
        reject(error)
      }
    })
  })

  // If we never see READY (older protocol), still resolve when port is known
  // and the /health endpoint answers.
  const fallback = new Promise<number>((resolve, reject) => {
    const interval = setInterval(async () => {
      if (resolved) return clearInterval(interval)
      if (port === null) return
      try {
        const response = await fetch(`http://127.0.0.1:${port}/health`)
        if (response.ok) {
          if (!resolved) {
            resolved = true
            clearInterval(interval)
            resolve(port)
          }
        }
      } catch {
        // sidecar not yet listening — keep polling
      }
    }, 250)
    setTimeout(() => {
      clearInterval(interval)
      if (!resolved) reject(new Error('Sidecar não respondeu /health a tempo.'))
    }, options.startTimeoutMs ?? DEFAULT_TIMEOUT_MS)
  })

  let resolvedPort: number
  try {
    resolvedPort = await Promise.race([ready, fallback])
  } catch (error) {
    if (child.exitCode === null) child.kill('SIGTERM')
    throw error
  }

  return {
    port: resolvedPort,
    baseUrl: `http://127.0.0.1:${resolvedPort}`,
    process: child,
    async shutdown() {
      if (child.exitCode !== null) return
      try {
        child.kill('SIGTERM')
        await Promise.race([
          once(child, 'exit'),
          new Promise((resolve) => setTimeout(resolve, 3_000))
        ])
        if (child.exitCode === null) child.kill('SIGKILL')
      } catch {
        // best-effort shutdown
      }
    }
  }
}

export const __test__ = { READY_TOKEN }
