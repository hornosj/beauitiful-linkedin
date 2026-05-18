import type { CSSProperties } from 'react'

export interface LiveActivityItem {
  id: string
  title: string
  detail?: string | null
  tone?: 'neutral' | 'success' | 'warning'
}

interface Props {
  items: LiveActivityItem[]
}

export default function LiveActivityBubbles(props: Props) {
  if (props.items.length === 0) return null

  return (
    <div className="live-activity-stack" role="status" aria-live="polite">
      {props.items.slice(0, 4).map((item, index) => (
        <div
          key={item.id}
          className="live-activity-bubble"
          data-tone={item.tone ?? 'neutral'}
          style={{ '--live-activity-index': index } as CSSProperties}
        >
          <span className="live-activity-dot" aria-hidden="true" />
          <span className="live-activity-copy">
            <strong>{item.title}</strong>
            {item.detail && <span>{item.detail}</span>}
          </span>
        </div>
      ))}
    </div>
  )
}
