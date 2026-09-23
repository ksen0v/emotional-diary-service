import { useNavigate } from 'react-router-dom'
import type { Incident } from '../lib/types'
import { OUTCOME } from './IncidentRow'

// Блок «Инциденты сегодня» из прототипа Main.dc.html: карточка с янтарной
// кромкой слева, внутри строки «время · что было · чип исхода».
//
// Строку собирает сервер — тот же сборщик, что и ленту раздела «Инциденты».
// Здесь показывается его короткая форма: в прототипе строка одна и условия
// снятия в неё не помещаются, а главное — на них тут не смотрят.
//
// Отступление от прототипа, названо: заголовок кликается и ведёт в раздел.
// В прототипе перехода отсюда нет, но раздел появился, и не дать в него
// попасть со строки, которую человек только что прочитал, было бы странно.
export function TodayIncidents({ items }: { items: Incident[] }) {
  const navigate = useNavigate()

  if (items.length === 0) {
    return (
      <div className="card" style={{ padding: '14px 18px' }}>
        <div className="klabel" style={{ marginBottom: 9 }}>
          Инциденты сегодня
        </div>
        <div className="hint">
          Сегодня инцидентов нет. Здесь появятся срабатывания правил и системных
          триггеров.
        </div>
      </div>
    )
  }

  return (
    <div
      className="card"
      style={{ padding: '14px 18px', borderLeft: '3px solid var(--warn)' }}
    >
      <div style={{ display: 'flex', alignItems: 'baseline', marginBottom: 9 }}>
        <span className="klabel">Инциденты сегодня</span>
        <button
          onClick={() => navigate('/incidents')}
          style={{
            marginLeft: 'auto',
            border: 'none',
            background: 'transparent',
            color: 'var(--accent)',
            fontSize: 12,
            padding: 0,
          }}
        >
          вся история
        </button>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 9 }}>
        {items.map((item) => {
          const tone = OUTCOME[item.outcome] ?? OUTCOME.open
          return (
            <div
              key={item.id}
              style={{ display: 'flex', alignItems: 'center', gap: 12 }}
            >
              <span
                className="mono"
                style={{ fontSize: 12, color: 'var(--faint)', flexShrink: 0 }}
              >
                {item.time_text}
              </span>
              <span style={{ fontSize: 13, minWidth: 0 }}>
                {item.title}
                {item.summary && (
                  <span style={{ color: 'var(--dim)' }}> · {item.summary}</span>
                )}
              </span>
              <span
                className="chip"
                style={{
                  marginLeft: 'auto',
                  flexShrink: 0,
                  color: tone.fg,
                  borderColor: tone.border,
                }}
              >
                {item.outcome_text}
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}
