import type { Incident } from '../lib/types'

// Строка ленты инцидентов из прототипа Incidents.dc.html. Живёт отдельным
// компонентом, потому что её использует и раздел «Инциденты», и блок
// «Инциденты сегодня» на «Сегодня»: одно событие должно выглядеть одинаково
// в обоих местах, иначе оно прочитается как два разных.
//
// Цвета взяты из прототипа: планка слева, цвет текста чипа и цвет его рамки.
// Третий исход — «идёт» — в прототипе не нарисован: инцидент, у которого
// блокировка ещё идёт, не соблюдён и не нарушен, и красить его в один из двух
// цветов значило бы объявить исход раньше времени.
export const OUTCOME: Record<
  string,
  { accent: string; fg: string; border: string }
> = {
  kept: { accent: '#3a5c45', fg: '#6fa97f', border: '#2f4a38' },
  breached: { accent: '#6b2e26', fg: '#e4897a', border: '#5c2a24' },
  open: { accent: '#4a4a42', fg: '#a9a69e', border: '#33332e' },
}

export function IncidentRow({
  item,
  last = false,
}: {
  item: Incident
  last?: boolean
}) {
  const tone = OUTCOME[item.outcome] ?? OUTCOME.open
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'flex-start',
        gap: 16,
        padding: '15px 0',
        borderBottom: last ? 'none' : '1px solid #212120',
      }}
    >
      <div style={{ width: 104, flexShrink: 0 }}>
        <div className="mono" style={{ fontSize: 13 }}>
          {item.time_text}
        </div>
        <div className="mono" style={{ fontSize: 11, color: 'var(--faint)' }}>
          {item.date_text}
        </div>
      </div>
      {/* Планка во всю высоту строки: по ней исход читается до того, как
          прочитан текст. */}
      <div
        style={{
          width: 6,
          flexShrink: 0,
          alignSelf: 'stretch',
          borderRadius: 3,
          background: tone.accent,
        }}
      />
      <div style={{ flexGrow: 1, minWidth: 0 }}>
        <div style={{ fontSize: 13, fontWeight: 500 }}>{item.title}</div>
        <div style={{ fontSize: 12, color: 'var(--faint)', marginTop: 4 }}>
          {item.detail}
        </div>
      </div>
      <span
        className="chip"
        style={{ color: tone.fg, borderColor: tone.border, whiteSpace: 'nowrap' }}
      >
        {item.outcome_text}
      </span>
    </div>
  )
}
