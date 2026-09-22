import type { Curve } from '../lib/types'
import { pct, pctPlain, time } from './format'

// Кривая дня — реконструкция из закрытых сделок, а не биржевой график:
// точка появляется на каждой сделке, а не через равные промежутки времени.
export function DayCurve({ curve, bare }: { curve: Curve; bare?: boolean }) {
  const points = curve.points
  // bare — кривая живёт внутри чужой карточки (главный экран собран так
  // в прототипе: плитки метрик и кривая — один блок, а не два).
  const Frame = bare ? Bare : Card
  if (points.length === 0) {
    return (
      <Frame>
        {!bare && (
          <div className="klabel" style={{ marginBottom: 8 }}>
            Кривая дня
          </div>
        )}
        <div className="hint">Сегодня закрытых сделок ещё нет.</div>
      </Frame>
    )
  }

  const width = 640
  const height = 130
  const pad = 8
  const values = points.map((p) => Number(p.equity_pct))
  const maxY = Math.max(0, ...values)
  const minY = Math.min(0, ...values)
  const span = maxY - minY || 1

  const x = (i: number) =>
    points.length === 1 ? width / 2 : pad + (i * (width - pad * 2)) / (points.length - 1)
  const y = (v: number) => pad + ((maxY - v) * (height - pad * 2)) / span

  const line = points.map((p, i) => `${x(i)},${y(Number(p.equity_pct))}`).join(' ')
  const zeroY = y(0)
  const last = points[points.length - 1]

  return (
    <Frame>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 14, marginBottom: 12 }}>
        <span className="klabel">Кривая дня</span>
        <span className="mono" style={{ fontSize: 13 }}>
          {pct(last.equity_pct)}
        </span>
        <span className="hint">
          пик {pct(curve.close.peak_pct)} · просадка {pctPlain(curve.close.max_drawdown_pct)}
        </span>
        <div style={{ flexGrow: 1 }} />
        {!curve.unrealized.available && (
          <span className="hint" title="Источник не отдаёт открытые позиции">
            без открытых позиций
          </span>
        )}
      </div>

      <svg
        viewBox={`0 0 ${width} ${height}`}
        style={{ width: '100%', height, display: 'block' }}
        role="img"
        aria-label={`Кривая дня, итог ${pct(last.equity_pct)}`}
      >
        <line x1={0} y1={zeroY} x2={width} y2={zeroY} stroke="#33332e" strokeWidth={1} />
        <polyline
          points={line}
          fill="none"
          stroke={Number(last.equity_pct) < 0 ? 'var(--bad)' : 'var(--ok)'}
          strokeWidth={1.5}
        />
        {points.map((p, i) => (
          <circle
            key={p.trade_id}
            cx={x(i)}
            cy={y(Number(p.equity_pct))}
            r={2.5}
            fill={Number(last.equity_pct) < 0 ? 'var(--bad)' : 'var(--ok)'}
          />
        ))}
      </svg>

      <div className="hint" style={{ display: 'flex', justifyContent: 'space-between' }}>
        <span>{time(points[0].at)}</span>
        <span>{time(last.at)}</span>
      </div>
    </Frame>
  )
}

function Card({ children }: { children: React.ReactNode }) {
  return (
    <div className="card" style={{ padding: '18px 20px' }}>
      {children}
    </div>
  )
}

function Bare({ children }: { children: React.ReactNode }) {
  return <div>{children}</div>
}
