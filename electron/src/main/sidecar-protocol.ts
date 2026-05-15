// Pure parser for the sidecar boot protocol.
// Kept free of node:child_process so it can be tested in jsdom.

export const PORT_TOKEN = 'BEAUTIFUL_LINKEDIN_PORT'
export const READY_TOKEN = 'BEAUTIFUL_LINKEDIN_READY'

export interface BootSnapshot {
  port: number | null
  ready: boolean
}

export function parseBootStdout(buffer: string): BootSnapshot {
  const portMatch = buffer.match(new RegExp(`${PORT_TOKEN}=(\\d+)`))
  const port = portMatch ? Number.parseInt(portMatch[1]!, 10) : null
  const ready = buffer.includes(READY_TOKEN)
  return { port, ready }
}
