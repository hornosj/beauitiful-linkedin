import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ApiClient } from '../src/shared/api'
import TelethonAuthDialog from '../src/renderer/src/components/TelethonAuthDialog'

describe('TelethonAuthDialog', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('releases the code confirmation step when sign-in stops responding', async () => {
    vi.useFakeTimers()

    const client = {
      sendTelethonAuthCode: vi.fn().mockResolvedValue({
        phone_code_hash: 'hash-123',
        next_type: null,
        timeout: null
      }),
      signInTelethonAuth: vi.fn().mockImplementation(() => new Promise(() => undefined))
    } as unknown as ApiClient

    render(
      <TelethonAuthDialog
        open
        client={client}
        onSuccess={vi.fn()}
        onClose={vi.fn()}
      />
    )

    fireEvent.change(screen.getByLabelText('Telefone'), {
      target: { value: '+5511999999999' }
    })
    fireEvent.click(screen.getByRole('button', { name: /enviar código/i }))

    await act(async () => undefined)

    fireEvent.change(screen.getByLabelText('Código'), {
      target: { value: '12345' }
    })
    fireEvent.click(screen.getByRole('button', { name: /confirmar código/i }))

    expect(screen.getByRole('button', { name: /confirmando/i })).toBeDisabled()

    await act(async () => {
      vi.advanceTimersByTime(30_001)
    })

    expect(screen.getByText(/tempo de resposta do Telegram expirou/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /confirmar código/i })).not.toBeDisabled()
  })
})
