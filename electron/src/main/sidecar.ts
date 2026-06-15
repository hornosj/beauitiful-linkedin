import { spawn, type ChildProcess } from 'node:child_process'
import { once } from 'node:events'
import { mkdirSync } from 'node:fs'
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

// Cold start do exe PyInstaller (onedir ~284MB) no 1º launch costuma estourar
// 20s quando o antivírus/SmartScreen varre cada arquivo de _internal/ ou o disco
// é lento — o que aparecia para o cliente como "Sidecar offline" mesmo o backend
// subindo logo em seguida. 60s dá folga; override via BEAUTIFUL_LINKEDIN_SIDECAR_TIMEOUT_MS.
const DEFAULT_TIMEOUT_MS = 60_000

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

// Cold start da JVM/Clojure (compila ~60 namespaces) é mais lento que o Python.
const CLJ_DEFAULT_TIMEOUT_MS = 90_000

function isCljMode(): boolean {
  return (process.env.BEAUTIFUL_LINKEDIN_SIDECAR ?? '').trim().toLowerCase() === 'clj'
}

// Diretório do projeto Clojure (irmão de beauitiful-linkedin). Override:
// BEAUTIFUL_LINKEDIN_CLJ_DIR. Em dev, options.projectRoot == .../beauitiful-linkedin.
function resolveCljDir(options: StartSidecarOptions): string {
  const explicit = (process.env.BEAUTIFUL_LINKEDIN_CLJ_DIR ?? '').trim()
  if (explicit) return explicit
  const base = options.projectRoot ?? options.cwd ?? process.cwd()
  return join(base, '..', 'beautiful-linkedin-clj-dev')
}

// Comando do sidecar Clojure. Defaults: `clojure -M:server`. Overrides:
// BEAUTIFUL_LINKEDIN_CLJ_CMD (ex.: java) e BEAUTIFUL_LINKEDIN_CLJ_ARGS
// (ex.: "-jar target/sidecar.jar" se você empacotar um uberjar).
function buildCljCommand(): SidecarCommand {
  const exe = (process.env.BEAUTIFUL_LINKEDIN_CLJ_CMD ?? '').trim() || 'clojure'
  const args = (process.env.BEAUTIFUL_LINKEDIN_CLJ_ARGS ?? '-M:server').trim().split(/\s+/)
  return { executable: exe, args }
}

// Cold start da JVM + Gradle (compila + sobe o Spring Boot) é mais lento que o
// Python; daí o timeout generoso.
const JAVA_DEFAULT_TIMEOUT_MS = 120_000

function isJavaMode(): boolean {
  return (process.env.BEAUTIFUL_LINKEDIN_SIDECAR ?? '').trim().toLowerCase() === 'java'
}

// Diretório do projeto Java (irmão de beauitiful-linkedin). Override:
// BEAUTIFUL_LINKEDIN_JAVA_DIR. Em dev, options.projectRoot == .../beauitiful-linkedin.
function resolveJavaDir(options: StartSidecarOptions): string {
  const explicit = (process.env.BEAUTIFUL_LINKEDIN_JAVA_DIR ?? '').trim()
  if (explicit) return explicit
  const base = options.projectRoot ?? options.cwd ?? process.cwd()
  return join(base, '..', 'beautiful-linkedin-java')
}

// Comando do sidecar Java. Default: `gradlew bootRun --console=plain` no projeto
// Java. Overrides: BEAUTIFUL_LINKEDIN_JAVA_CMD (ex.: java) e
// BEAUTIFUL_LINKEDIN_JAVA_ARGS (ex.: "-jar build/libs/beautiful-linkedin-java-0.1.0.jar").
function buildJavaCommand(javaDir: string): SidecarCommand {
  const cmdOverride = (process.env.BEAUTIFUL_LINKEDIN_JAVA_CMD ?? '').trim()
  const argsOverride = (process.env.BEAUTIFUL_LINKEDIN_JAVA_ARGS ?? '').trim()
  if (cmdOverride) {
    return { executable: cmdOverride, args: argsOverride ? argsOverride.split(/\s+/) : [] }
  }
  const gradleArgs = (argsOverride || 'bootRun --console=plain').split(/\s+/)
  if (process.platform === 'win32') {
    // Node não executa .bat diretamente — passa pelo cmd.exe.
    const gradlew = join(javaDir, 'gradlew.bat')
    return {
      executable: process.env.ComSpec ?? 'cmd.exe',
      args: ['/d', '/s', '/c', gradlew, ...gradleArgs]
    }
  }
  return { executable: join(javaDir, 'gradlew'), args: gradleArgs }
}

export async function startSidecar(options: StartSidecarOptions = {}): Promise<SidecarHandle> {
  // Modo Clojure (dev): BEAUTIFUL_LINKEDIN_SIDECAR=clj sobe `clojure -M:server`
  // no projeto irmão. Mesmo handshake PORT/READY no stdout, então renderer e o
  // resto do main não mudam.
  if (isCljMode()) {
    const cljDir = resolveCljDir(options)
    const command = buildCljCommand()
    options.onLog?.(
      `[sidecar] modo CLOJURE: ${command.executable} ${command.args.join(' ')} (cwd=${cljDir})`
    )
    return startSidecarWithCommand(command, {
      ...options,
      cwd: cljDir,
      startTimeoutMs: options.startTimeoutMs ?? CLJ_DEFAULT_TIMEOUT_MS
    })
  }

  // Modo Java (dev): BEAUTIFUL_LINKEDIN_SIDECAR=java sobe o sidecar Spring Boot
  // via Gradle (gradlew bootRun) no projeto irmão beautiful-linkedin-java. Mesmo
  // handshake PORT/READY no stdout, então renderer e o resto do main não mudam.
  if (isJavaMode()) {
    const javaDir = resolveJavaDir(options)
    const command = buildJavaCommand(javaDir)
    // O caminho do SQLite é relativo ao projeto Java; garante que data/ existe e
    // força um caminho absoluto para não depender do cwd do bootRun.
    const savedLeadsPath = join(javaDir, 'data', 'saved_leads.sqlite')
    try {
      mkdirSync(join(javaDir, 'data'), { recursive: true })
    } catch {
      // best-effort
    }
    options.onLog?.(
      `[sidecar] modo JAVA: ${command.executable} ${command.args.join(' ')} (cwd=${javaDir})`
    )
    const port =
      options.env?.BEAUTIFUL_LINKEDIN_PORT ?? process.env.BEAUTIFUL_LINKEDIN_PORT ?? '0'
    return startSidecarWithCommand(command, {
      ...options,
      cwd: javaDir,
      env: {
        ...(options.env ?? {}),
        // Porta efêmera (0) como o sidecar Python — a porta real volta no stdout.
        BEAUTIFUL_LINKEDIN_PORT: port,
        BEAUTIFUL_LINKEDIN_SAVED_LEADS_PATH: savedLeadsPath
      },
      startTimeoutMs: options.startTimeoutMs ?? JAVA_DEFAULT_TIMEOUT_MS
    })
  }

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
