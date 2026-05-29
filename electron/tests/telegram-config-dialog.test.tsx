import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ApiClient } from '../src/shared/api'
import TelegramConfigDialog from '../src/renderer/src/components/TelegramConfigDialog'

describe('TelegramConfigDialog', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('persists credentials and advances to the phone step on save', async () => {
    const saveTelethonConfig = vi.fn().mockResolvedValue({
      authorized: false,
      configured: true,
      session_name: 'data/telegram_phone_lookup'
    })
    const client = { saveTelethonConfig } as unknown as ApiClient
    const onSaved = vi.fn()

    render(
      <TelegramConfigDialog open client={client} onSaved={onSaved} onClose={vi.fn()} />
    )

    fireEvent.change(screen.getByLabelText('API ID'), { target: { value: '1234567' } })
    fireEvent.change(screen.getByLabelText('API Hash'), { target: { value: 'deadbeef' } })
    fireEvent.click(screen.getByRole('button', { name: /salvar e continuar/i }))

    await act(async () => undefined)

    expect(saveTelethonConfig).toHaveBeenCalledWith({
      api_id: '1234567',
      api_hash: 'deadbeef'
    })
    expect(onSaved).toHaveBeenCalledTimes(1)
  })

  it('blocks a non-numeric API ID without calling the API', async () => {
    const saveTelethonConfig = vi.fn()
    const client = { saveTelethonConfig } as unknown as ApiClient
    const onSaved = vi.fn()

    render(
      <TelegramConfigDialog open client={client} onSaved={onSaved} onClose={vi.fn()} />
    )

    fireEvent.change(screen.getByLabelText('API ID'), { target: { value: 'abc' } })
    fireEvent.change(screen.getByLabelText('API Hash'), { target: { value: 'deadbeef' } })
    fireEvent.click(screen.getByRole('button', { name: /salvar e continuar/i }))

    await act(async () => undefined)

    expect(saveTelethonConfig).not.toHaveBeenCalled()
    expect(onSaved).not.toHaveBeenCalled()
    expect(screen.getByText(/apenas números/i)).toBeInTheDocument()
  })
})
