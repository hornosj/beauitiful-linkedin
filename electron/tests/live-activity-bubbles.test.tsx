import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import LiveActivityBubbles from '../src/renderer/src/components/LiveActivityBubbles'

describe('LiveActivityBubbles', () => {
  it('renders live black-bubble activity messages one by one', () => {
    render(
      <LiveActivityBubbles
        items={[
          {
            id: 'lead-1',
            title: 'Lead encontrado',
            detail: 'Ana Silva · Head of Marketing',
            tone: 'success'
          }
        ]}
      />
    )

    expect(screen.getByRole('status')).toHaveTextContent('Lead encontrado')
    expect(screen.getByText('Ana Silva · Head of Marketing')).toBeInTheDocument()
  })

  it('keeps the stack compact when many events arrive', () => {
    render(
      <LiveActivityBubbles
        items={[0, 1, 2, 3, 4].map((index) => ({
          id: `item-${index}`,
          title: `Evento ${index}`
        }))}
      />
    )

    expect(screen.getAllByText(/Evento/)).toHaveLength(4)
    expect(screen.queryByText('Evento 4')).not.toBeInTheDocument()
  })
})
