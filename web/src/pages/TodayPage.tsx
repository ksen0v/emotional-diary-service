import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ApiError, api } from '../lib/api'
import type { Curve, Feed, MarkingMetrics, Today } from '../lib/types'
import { DayCurve } from '../ui/DayCurve'
import { EntryEditor } from '../ui/EntryEditor'
import { Stub } from '../ui/Stub'
import { duration, money, pct, pnlColor, time } from '../ui/format'

export function useToday() {
  return useQuery<Today>({
    queryKey: ['today'],
    queryFn: () => api.get<Today>('/today'),
    // Экран живёт весь торговый день, поэтому перечитываем сами: без этого
    // граница дня и закрытие сессии появились бы только после перезагрузки.
    refetchInterval: 60_000,
  })
}

export function TodayPage() {
  const today = useToday()

  if (today.isLoading) {
    return <div className="hint">загрузка…</div>
  }
  if (!today.data) {
    return <div className="err">Не удалось получить состояние дня.</div>
  }
  if (today.data.state === 'review_pending') {
    return <ReviewPending today={today.data} />
  }
  if (today.data.state === 'check_failed') {
    return <NoAdmission today={today.data} />
  }
  return <DayScreen today={today.data} />
}

// Э-08: перекрытие «разбор за вчера не заполнен». Одна кнопка, других путей нет.
// Жёсткость сознательная (ОВ-15): разбор — условие следующей сессии.
function ReviewPending({ today }: { today: Today }) {
  const navigate = useNavigate()
  const day = today.review.pending_day
  return (
    <div style={{ maxWidth: 520, paddingTop: 50, textAlign: 'center', margin: '0 auto' }}>
      <div style={{ fontSize: 19, marginBottom: 14 }}>
        Разбор за{' '}
        {day
          ? new Date(day).toLocaleDateString('ru-RU', { day: 'numeric', month: 'long' })
          : 'прошлый день'}{' '}
        не заполнен
      </div>
      <div className="hint" style={{ marginBottom: 26, lineHeight: 1.8 }}>
        Пока он не заполнен, чек готовности недоступен.
      </div>
      <button className="primary" onClick={() => navigate(`/review/${day}`)}>
        Заполнить разбор
      </button>
    </div>
  )
}

// Э-06: перекрытие «нет допуска». Полный экран, без выхода в работу.
// Сухо, без морали — работает сам факт закрытой двери.
function NoAdmission({ today }: { today: Today }) {
  const navigate = useNavigate()
  return (
    <div style={{ maxWidth: 520, paddingTop: 40, textAlign: 'center', margin: '0 auto' }}>
      <div style={{ fontSize: 20, marginBottom: 18 }}>Допуска на сегодня нет</div>
      <div className="mono" style={{ fontSize: 40 }}>
        {today.admission?.score} / 25
      </div>
      <div className="hint" style={{ marginBottom: 22 }}>
        порог допуска — {today.thresholds.min_score}
      </div>
      <div style={{ color: 'var(--dim)', lineHeight: 1.9, marginBottom: 26 }}>
        Сессия не откроется. Перепройти чек сегодня нельзя.
        <br />
        Любая сделка, открытая сегодня, будет зафиксирована как инцидент.
      </div>
      <div style={{ display: 'flex', gap: 10, justifyContent: 'center' }}>
        <button onClick={() => navigate('/diary')}>Дневник</button>
        <button onClick={() => navigate('/trades')}>История</button>
      </div>
      <div className="hint" style={{ marginTop: 26 }}>
        Инцидент «торговля без допуска» появится на шаге 10 — пока сервис только
        не открывает сессию.
      </div>
    </div>
  )
}

function DayScreen({ today }: { today: Today }) {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const now = useServerClock(today.server_time)
  const open = today.state === 'trading'
  const dim = open ? 1 : 0.45

  const curve = useQuery<Curve>({
    queryKey: ['curve'],
    queryFn: () => api.get<Curve>('/trades/day-curve'),
  })
  const metrics = useQuery<MarkingMetrics>({
    queryKey: ['marking-metrics', 'today'],
    queryFn: () => api.get<MarkingMetrics>('/trades/metrics?period=today'),
    enabled: today.source !== null,
  })
  const feed = useQuery<Feed>({
    queryKey: ['trades', 'today', 'all', null],
    queryFn: () => api.get<Feed>('/trades?period=today&filter=all&limit=5'),
    enabled: today.source !== null,
  })

  const closeSession = useMutation({
    mutationFn: () => api.post('/session/close'),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['today'] }),
    onError: (err) =>
      alertless(err instanceof ApiError ? err.message : 'Не получилось закрыть сессию.'),
  })
  const [closeError, setCloseError] = useState('')
  function alertless(message: string) {
    setCloseError(message)
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16, maxWidth: 1000 }}>
      <StatusBlock
        today={today}
        now={now}
        onCheck={() => navigate('/premarket')}
        onClose={() => closeSession.mutate()}
        onReview={() => navigate(`/review/${today.day}`)}
        closing={closeSession.isPending}
        closeError={closeError}
      />

      {today.attention.length > 0 && (
        <div className="card" style={{ padding: '12px 18px' }}>
          {today.attention.map((item) => (
            <div key={item.code} style={{ fontSize: 13, color: 'var(--warn)' }}>
              {item.message}
            </div>
          ))}
        </div>
      )}

      <div style={{ opacity: dim, display: 'flex', flexDirection: 'column', gap: 16 }}>
        <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', alignItems: 'stretch' }}>
          <div style={{ flex: '1 1 460px', minWidth: 320 }}>
            {curve.data && <DayCurve curve={curve.data} />}
          </div>
          <div className="card" style={{ flex: '1 1 260px', padding: '18px 20px' }}>
            <div className="klabel" style={{ marginBottom: 14 }}>
              Метрики дня
            </div>
            <Rows
              rows={[
                ['Результат', money(today.counters.profit_usd), pnlColor(today.counters.profit_usd)],
                ['От депозита', pct(today.counters.equity_pct), pnlColor(today.counters.equity_pct)],
                ['Сделок', String(today.counters.all_trades), undefined],
                ['Значимых', String(today.counters.significant_trades), undefined],
                [
                  'Нарушений',
                  String(today.counters.violations),
                  today.counters.violations > 0 ? 'var(--bad)' : undefined,
                ],
                [
                  'Просадка от пика',
                  pct(today.counters.drawdown_pct),
                  Number(today.counters.drawdown_pct) > 0 ? 'var(--warn)' : undefined,
                ],
                [
                  'Убытков подряд',
                  String(today.counters.loss_streak),
                  today.counters.loss_streak > 1 ? 'var(--bad)' : undefined,
                ],
                [
                  'Цена эмоций',
                  metrics.data ? money(metrics.data.marking.emotion_cost_usd) : '—',
                  metrics.data
                    ? pnlColor(metrics.data.marking.emotion_cost_usd)
                    : undefined,
                ],
                [
                  'Покрытие разметкой',
                  metrics.data
                    ? `${Number(metrics.data.marking.coverage_pct).toFixed(0)}%`
                    : '—',
                  undefined,
                ],
              ]}
            />
          </div>
        </div>

        <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
          <div style={{ flex: '1 1 460px', minWidth: 320 }}>
            <Stub
              title="Ближе всего к срабатыванию"
              step={9}
              what="Правило с полосой прогресса: видно, что подходишь к границе, до того как её пересечёшь."
            />
          </div>
          <div style={{ flex: '1 1 260px' }}>
            <Stub title="Стрик" step={7} what="Дни подряд без нарушений и лучший результат." />
          </div>
        </div>

        <div className="card" style={{ padding: '4px 0' }}>
          <div className="klabel" style={{ padding: '14px 18px 8px' }}>
            Последние сделки
          </div>
          {(feed.data?.items ?? []).length === 0 ? (
            <div className="hint" style={{ padding: '0 18px 16px' }}>
              Сегодня сделок нет.
            </div>
          ) : (
            (feed.data?.items ?? []).map((trade) => (
              <div
                key={trade.id}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 14,
                  padding: '9px 18px',
                  borderTop: '1px solid #212120',
                  fontSize: 13,
                }}
              >
                <span className="mono" style={{ color: 'var(--dim)', width: 44 }}>
                  {time(trade.close_time)}
                </span>
                <span style={{ width: 92, fontWeight: 500 }}>{trade.symbol}</span>
                <span style={{ width: 48, color: 'var(--dim)' }}>
                  {trade.side === 'long' ? 'лонг' : 'шорт'}
                </span>
                <span
                  className="mono"
                  style={{ width: 86, textAlign: 'right', color: pnlColor(trade.profit_usd) }}
                >
                  {money(trade.profit_usd)}
                </span>
                <span style={{ width: 66, color: 'var(--faint)' }}>
                  {duration(trade.duration_sec)}
                </span>
                <div style={{ flexGrow: 1 }} />
                <span className="hint">
                  {trade.marking === 'unreviewed' ? 'без разметки' : trade.tags
                    .map((t) => t.name)
                    .join(', ')}
                </span>
              </div>
            ))
          )}
          <div style={{ padding: '10px 18px' }}>
            <button
              onClick={() => navigate('/trades')}
              style={{ fontSize: 12, padding: '6px 12px' }}
            >
              Разметить
            </button>
          </div>
        </div>

        <EntryEditor level="day" periodStart={today.day} entry={today.entry} />
      </div>
    </div>
  )
}

const LABELS: Record<string, { label: string; color: string }> = {
  green: { label: 'Зелёный допуск', color: 'var(--ok)' },
  red: { label: 'Допуск под риском', color: 'var(--warn)' },
  denied: { label: 'Нет допуска', color: 'var(--bad)' },
}

function StatusBlock({
  today,
  now,
  onCheck,
  onClose,
  onReview,
  closing,
  closeError,
}: {
  today: Today
  now: Date
  onCheck: () => void
  onClose: () => void
  onReview: () => void
  closing: boolean
  closeError: string
}) {
  const admission = today.admission
  const label = admission ? LABELS[admission.verdict] : null

  if (today.state === 'no_source') {
    return (
      <div className="card" style={{ padding: '22px 24px' }}>
        <div style={{ fontSize: 16, marginBottom: 8 }}>Источник сделок не подключён</div>
        <div className="hint">
          Пока сервис не видит сделок, считать нечего. Вставь ключ TMM на экране
          настроек — или подключи там тестовый источник.
        </div>
      </div>
    )
  }

  if (today.state === 'no_check') {
    return (
      <div className="card" style={{ padding: '28px 24px' }}>
        <div className="serif" style={{ fontSize: 24, marginBottom: 8 }}>
          Сессия не открыта
        </div>
        <div className="hint" style={{ marginBottom: 20, maxWidth: 520 }}>
          Сессии не существует, пока не пройден чек готовности. Пять вопросов,
          минута времени. День кончается в{' '}
          {new Date(today.day_ends_at).toLocaleTimeString('ru-RU', {
            hour: '2-digit',
            minute: '2-digit',
          })}
          .
        </div>
        <button className="primary" onClick={onCheck}>
          Пройти чек готовности
        </button>
      </div>
    )
  }

  if (today.state === 'session_closed') {
    return (
      <div className="card" style={{ padding: '22px 24px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 16 }}>Сессия закрыта</span>
          {label && <span style={{ color: label.color, fontSize: 13 }}>{label.label}</span>}
          <span className="hint">
            {today.session.closed_at ? `в ${time(today.session.closed_at)}` : ''}
          </span>
        </div>
        <div className="hint" style={{ marginTop: 10 }}>
          Итог дня: {money(today.counters.profit_usd)} ·{' '}
          {today.counters.all_trades} сделок · нарушений {today.counters.violations}.
        </div>
        {today.review.state === 'pending' ? (
          <div style={{ marginTop: 14, display: 'flex', gap: 10, alignItems: 'center' }}>
            <button className="primary" onClick={onReview} style={{ fontSize: 12, padding: '6px 12px' }}>
              Заполнить разбор
            </button>
            <span className="hint">
              Без разбора завтрашний чек не даётся. Лучше сегодня, пока помнишь.
            </span>
          </div>
        ) : (
          <div className="hint" style={{ marginTop: 10 }}>
            Разбор за этот день заполнен.
          </div>
        )}
      </div>
    )
  }

  const opened = today.session.opened_at ? new Date(today.session.opened_at) : null
  const elapsed = opened ? Math.floor((now.getTime() - opened.getTime()) / 1000) : null

  return (
    <div className="card" style={{ padding: '22px 24px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 14, flexWrap: 'wrap' }}>
        <span
          style={{
            width: 9,
            height: 9,
            borderRadius: 5,
            background: label?.color ?? 'var(--dim)',
          }}
        />
        <span style={{ fontSize: 16 }}>{label?.label}</span>
        <span className="hint">
          сессия открыта в {opened ? time(today.session.opened_at) : '—'}
          {elapsed !== null ? ` · ${duration(elapsed)}` : ''}
        </span>
        <div style={{ flexGrow: 1 }} />
        <span className="hint">
          балл {admission?.score} из 25 · день до{' '}
          {new Date(today.day_ends_at).toLocaleTimeString('ru-RU', {
            hour: '2-digit',
            minute: '2-digit',
          })}
        </span>
      </div>

      {admission?.verdict === 'red' && (
        <div
          style={{
            marginTop: 14,
            padding: '10px 12px',
            border: '1px solid #4a4326',
            borderRadius: 8,
            fontSize: 13,
            color: 'var(--warn)',
          }}
        >
          Под риском: рекомендация — половина обычного размера позиции. Сервис
          не урезает размер сам, это напоминание.
        </div>
      )}

      <div style={{ marginTop: 16, display: 'flex', gap: 10, alignItems: 'center' }}>
        <button onClick={onClose} disabled={closing} style={{ fontSize: 12, padding: '6px 12px' }}>
          {closing ? 'Закрываю…' : 'Закрыть сессию'}
        </button>
        <span className="hint">
          Раньше границы дня — чтобы разбор случился сегодня, а не завтра.
        </span>
      </div>
      {closeError && (
        <div className="err" style={{ marginTop: 10 }}>
          {closeError}
        </div>
      )}
    </div>
  )
}

function Rows({ rows }: { rows: [string, string, string | undefined][] }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      {rows.map(([label, value, color]) => (
        <div key={label} style={{ display: 'flex', justifyContent: 'space-between' }}>
          <span className="hint">{label}</span>
          <span className="mono" style={{ fontSize: 13, color: color ?? 'var(--fg)' }}>
            {value}
          </span>
        </div>
      ))}
    </div>
  )
}

// Часы сервера: таймеры считаются от server_time, а не от Date.now() —
// иначе при сбитых часах на машине трейдера сессия «шла» бы не столько,
// сколько на самом деле (решение Архитектуры ч.2 §3.5).
function useServerClock(serverTime: string): Date {
  const [skew] = useState(() => new Date(serverTime).getTime() - Date.now())
  const [now, setNow] = useState(() => new Date(Date.now() + skew))
  useEffect(() => {
    const id = setInterval(() => setNow(new Date(Date.now() + skew)), 15_000)
    return () => clearInterval(id)
  }, [skew])
  return now
}
