import type { NearRule } from '../lib/types'

// Блок «Ближе всего к срабатыванию» с экрана «Сегодня», собран по прототипу
// Main.dc.html. Смысл блока — превратить правила из невидимой сетки
// в приборную панель: видно, что подходишь к границе, до того как её
// пересечёшь. Это и есть цель сервиса — трение до сделки, а не наказание после.
//
// Все числа и строку «2.9% из 5%» считает сервер: фронт не знает ни приоритета
// связок «и» / «или», ни того, какое из условий держит правило.

const HOT = '#D9A03F'
const CALM = '#6E6C65'

export function NearRules({ rules, note }: { rules: NearRule[]; note?: string }) {
  return (
    <div className="card" style={{ padding: '14px 18px' }}>
      <div className="klabel" style={{ marginBottom: 11 }}>
        Ближе всего к срабатыванию
      </div>

      {rules.length === 0 ? (
        <div className="hint">{note ?? 'Правил с числовыми условиями пока нет.'}</div>
      ) : (
        rules.map((rule, i) => (
          <div
            key={rule.rule_id}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 12,
              marginBottom: i === rules.length - 1 ? 0 : 9,
            }}
          >
            <span
              style={{
                fontSize: 13,
                width: 250,
                flexShrink: 0,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
              title={rule.metric_name}
            >
              {rule.name}
            </span>
            <span
              className="mono"
              style={{ fontSize: 12, color: 'var(--dim)', width: 86, flexShrink: 0 }}
            >
              {rule.value_text}
            </span>
            <span
              style={{
                flexGrow: 1,
                height: 6,
                borderRadius: 3,
                background: 'var(--line)',
                position: 'relative',
                minWidth: 60,
              }}
            >
              <span
                style={{
                  position: 'absolute',
                  left: 0,
                  top: 0,
                  bottom: 0,
                  width: `${Math.round(Number(rule.ratio) * 100)}%`,
                  borderRadius: 3,
                  background: rule.met ? 'var(--bad)' : rule.hot ? HOT : CALM,
                }}
              />
            </span>
          </div>
        ))
      )}
    </div>
  )
}
