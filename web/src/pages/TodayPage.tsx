import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ApiError, api } from '../lib/api'
import type { Curve, Feed, Today } from '../lib/types'
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

const ADMISSION: Record<string, { label: string; color: string }> = {
  green: { label: 'Зелёный допуск', color: 'var(--ok)' },
  red: { label: 'Допуск под риском', color: 'var(--warn)' },
  denied: { label: 'Нет допуска', color: 'var(--bad)' },
}

export function TodayPage() {
  const today = useToday()

  if (today.isLoading) return <div className="hint">загрузка…</div>
  if (!today.data) return <div className="err">Не удалось получить состояние дня.</div>

  const state = today.data.state
  if (state === 'review_pending') return <ReviewPending today={today.data} />
  if (state === 'check_failed') return <NoAdmission today={today.data} />
  if (state === 'no_source') return <NoSource />
  if (state === 'no_check') return <PreSession today={today.data} />
  return <DayScreen today={today.data} />
}

// Э-08: перекрытие «разбор за вчера не заполнен». Одна кнопка, других путей нет.
function ReviewPending({ today }: { today: Today }) {
  const navigate = useNavigate()
  const day = today.review.pending_day
  return (
    <Centered>
      <div style={{ textAlign: 'center', maxWidth: 480 }}>
        <div style={{ fontSize: 19, marginBottom: 14 }}>
          Разбор за {day ? humanDay(day) : 'прошлый день'} не заполнен
        </div>
        <div className="hint" style={{ marginBottom: 28, lineHeight: 1.8 }}>
          Пока он не заполнен, чек готовности недоступен.
        </div>
        <button className="cta" onClick={() => navigate(`/review/${day}`)}>
          Заполнить разбор
        </button>
      </div>
    </Centered>
  )
}

// Э-06: допуска нет. Полный экран, сухо, без морали — работает сам факт
// закрытой двери. Просадившие ответы показываем и после перезагрузки.
function NoAdmission({ today }: { today: Today }) {
  const navigate = useNavigate()
  const admission = today.admission
  return (
    <Centered>
      <div style={{ textAlign: 'center', maxWidth: 520 }}>
        <div style={{ fontSize: 19 }}>Допуска на сегодня нет</div>
        <div className="mono" style={{ fontSize: 38, marginTop: 24 }}>
          {admission?.score}
          <span style={{ color: 'var(--faint)' }}> / {admission?.max_score ?? 25}</span>
        </div>
        <div className="mono hint" style={{ marginTop: 6 }}>
          порог допуска — {today.thresholds.min_score}
        </div>

        {(admission?.weak.length ?? 0) > 0 && (
          <div
            className="card"
            style={{
              width: 380,
              maxWidth: '100%',
              margin: '30px auto 0',
              padding: '18px 22px',
              textAlign: 'left',
            }}
          >
            <div className="klabel" style={{ marginBottom: 10 }}>
              Балл просадили
            </div>
            {admission?.weak.map((w) => (
              <div key={w.id} style={{ display: 'flex', fontSize: 13, padding: '3px 0' }}>
                <span>{w.short}</span>
                <span
                  className="mono"
                  style={{
                    marginLeft: 'auto',
                    color: w.points <= 1 ? 'var(--bad)' : 'var(--warn)',
                  }}
                >
                  {w.points} / {w.max}
                </span>
              </div>
            ))}
          </div>
        )}

        <div style={{ fontSize: 14, color: 'var(--dim)', marginTop: 30, lineHeight: 1.7 }}>
          Сессия не откроется. Перепройти чек сегодня нельзя.
          <br />
          Любая сделка, открытая сегодня, будет зафиксирована как инцидент.
        </div>

        <div style={{ display: 'flex', gap: 12, justifyContent: 'center', marginTop: 34 }}>
          <button onClick={() => navigate('/diary')} style={{ padding: '12px 22px' }}>
            Дневник
          </button>
          <button onClick={() => navigate('/trades')} style={{ padding: '12px 22px' }}>
            История
          </button>
        </div>
        <div className="mono hint" style={{ marginTop: 40 }}>
          Инцидент «торговля без допуска» появится на шаге 10
        </div>
      </div>
    </Centered>
  )
}

function NoSource() {
  const navigate = useNavigate()
  return (
    <div className="card" style={{ padding: '40px 44px', maxWidth: 720 }}>
      <div className="serif" style={{ fontSize: 34, lineHeight: 1.15 }}>
        Источник сделок не подключён
      </div>
      <div
        style={{ fontSize: 14, color: 'var(--dim)', marginTop: 14, maxWidth: 480, lineHeight: 1.55 }}
      >
        Пока сервис не видит сделок, считать нечего: ни допуска, ни метрик дня.
        Вставь ключ TMM на экране настроек — или подключи там тестовый источник.
      </div>
      <button className="cta" style={{ marginTop: 26 }} onClick={() => navigate('/settings')}>
        К настройкам
      </button>
    </div>
  )
}

// Э-04 до чека: главный блок занимает пол-экрана, остальное приглушено.
function PreSession({ today }: { today: Today }) {
  const navigate = useNavigate()
  const y = today.yesterday
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
      <div
        className="card"
        style={{ padding: '40px 44px', display: 'flex', gap: 44, flexWrap: 'wrap' }}
      >
        <div style={{ flexGrow: 1, minWidth: 320 }}>
          <div className="serif" style={{ fontSize: 40, lineHeight: 1.15 }}>
            Сессия не открыта
          </div>
          <div
            style={{
              fontSize: 14,
              color: 'var(--dim)',
              marginTop: 14,
              maxWidth: 480,
              lineHeight: 1.55,
            }}
          >
            Чек готовности — пять вопросов, минута. До него сессии нет: сделки
            будут считаться торговлей без допуска.
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 18, marginTop: 26 }}>
            <button className="cta" onClick={() => navigate('/premarket')}>
              Пройти чек готовности
            </button>
            <span className="mono hint">
              {humanDay(today.day)} · день до{' '}
              {new Date(today.day_ends_at).toLocaleTimeString('ru-RU', {
                hour: '2-digit',
                minute: '2-digit',
              })}
            </span>
          </div>
        </div>
        <div
          style={{
            width: 232,
            flexShrink: 0,
            borderLeft: '1px solid var(--line)',
            paddingLeft: 28,
          }}
        >
          <div className="klabel" style={{ marginBottom: 10 }}>
            Вчера
          </div>
          <div style={{ fontSize: 13, lineHeight: 1.85 }}>
            <YRow label="PnL" value={money(y.profit_usd)} color={pnlColor(y.profit_usd)} />
            <YRow label="Сделок" value={String(y.trades)} />
            <YRow
              label="Нарушений"
              value={String(y.violations)}
              color={y.violations > 0 ? 'var(--bad)' : undefined}
            />
            <YRow
              label="Разбор"
              value={
                y.review_state === 'done'
                  ? 'заполнен'
                  : y.review_state === 'pending'
                    ? 'не заполнен'
                    : '—'
              }
              color={y.review_state === 'done' ? 'var(--ok)' : undefined}
            />
          </div>
        </div>
      </div>

      <div style={{ opacity: 0.38, display: 'flex', flexDirection: 'column', gap: 16 }}>
        <div className="card" style={{ padding: '16px 18px' }}>
          <Tiles today={today} empty />
          <div className="hint" style={{ marginTop: 10 }}>
            Кривая дня появится с первой закрытой сделкой.
          </div>
        </div>
        <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
          <div className="card" style={{ width: 636, maxWidth: '100%', padding: '14px 18px' }}>
            <div className="klabel">Ближе всего к срабатыванию</div>
            <div className="hint" style={{ marginTop: 10 }}>
              Правила начнут считаться после открытия сессии
            </div>
          </div>
          <div className="card" style={{ flexGrow: 1, minWidth: 260, padding: '14px 18px' }}>
            <div className="klabel">Запись за сегодня</div>
            <div className="hint" style={{ marginTop: 10 }}>
              Доступна после чека
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

function YRow({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div style={{ display: 'flex' }}>
      <span style={{ color: 'var(--dim)' }}>{label}</span>
      <span className="mono" style={{ marginLeft: 'auto', color: color ?? 'var(--fg)' }}>
        {value}
      </span>
    </div>
  )
}

function DayScreen({ today }: { today: Today }) {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const now = useServerClock(today.server_time)
  const [closeError, setCloseError] = useState('')

  const closed = today.state === 'session_closed'
  const label = today.admission ? ADMISSION[today.admission.verdict] : null

  const curve = useQuery<Curve>({
    queryKey: ['curve'],
    queryFn: () => api.get<Curve>('/trades/day-curve'),
  })
  const feed = useQuery<Feed>({
    queryKey: ['trades', 'today', 'all', null],
    queryFn: () => api.get<Feed>('/trades?period=today&filter=all&limit=5'),
  })

  const closeSession = useMutation({
    mutationFn: () => api.post('/session/close'),
    onSuccess: () => {
      setCloseError('')
      qc.invalidateQueries({ queryKey: ['today'] })
    },
    onError: (err) =>
      setCloseError(err instanceof ApiError ? err.message : 'Не получилось закрыть сессию.'),
  })

  const opened = today.session.opened_at ? new Date(today.session.opened_at) : null
  const elapsed = opened ? Math.floor((now.getTime() - opened.getTime()) / 1000) : null
  const dayEnds = new Date(today.day_ends_at).toLocaleTimeString('ru-RU', {
    hour: '2-digit',
    minute: '2-digit',
  })

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div
        className="card"
        style={{
          padding: '14px 18px',
          display: 'flex',
          alignItems: 'center',
          gap: 16,
          flexWrap: 'wrap',
          borderLeft: `3px solid ${label?.color ?? 'var(--line-2)'}`,
        }}
      >
        <div style={{ flexGrow: 1, minWidth: 220 }}>
          <div style={{ fontSize: 15, fontWeight: 500 }}>
            {closed
              ? `Сессия закрыта в ${time(today.session.closed_at)}`
              : `Сессия открыта в ${time(today.session.opened_at)}`}
            {label && (
              <span style={{ color: label.color, fontSize: 13, marginLeft: 10 }}>
                {label.label}
              </span>
            )}
          </div>
          <div className="hint" style={{ marginTop: 3 }}>
            {closed
              ? `Итог дня: ${money(today.counters.profit_usd)} · ${today.counters.all_trades} сделок · нарушений ${today.counters.violations}`
              : `Идёт ${elapsed !== null ? duration(elapsed) : '—'} · закроется автоматически в ${dayEnds} · балл допуска ${today.admission?.score} из ${today.admission?.max_score ?? 25}`}
          </div>
        </div>
        {closed ? (
          today.review.state === 'pending' ? (
            <button className="cta" onClick={() => navigate(`/review/${today.day}`)}>
              Заполнить разбор
            </button>
          ) : (
            <span className="hint">Разбор за этот день заполнен</span>
          )
        ) : (
          <button
            onClick={() => closeSession.mutate()}
            disabled={closeSession.isPending}
            style={{ fontSize: 13 }}
          >
            {closeSession.isPending ? 'Закрываю…' : 'Завершить сессию'}
          </button>
        )}
      </div>

      {closeError && <div className="err">{closeError}</div>}

      {today.admission?.verdict === 'red' && !closed && (
        <div
          className="card"
          style={{ padding: '12px 18px', borderLeft: '3px solid var(--warn)', fontSize: 13 }}
        >
          Под риском: рекомендация — половина обычного размера позиции. Сервис
          не урезает размер сам, это напоминание.
        </div>
      )}

      {today.attention.length > 0 && (
        <div className="card" style={{ padding: '12px 18px' }}>
          {today.attention.map((item) => (
            <div key={item.code} style={{ fontSize: 13, color: 'var(--warn)' }}>
              {item.message}
            </div>
          ))}
        </div>
      )}

      <div className="card" style={{ padding: '16px 18px' }}>
        <Tiles today={today} />
        {curve.data && <DayCurve curve={curve.data} bare />}
      </div>

      <div style={{ display: 'flex', gap: 16, alignItems: 'flex-start', flexWrap: 'wrap' }}>
        <div
          style={{
            width: 636,
            maxWidth: '100%',
            display: 'flex',
            flexDirection: 'column',
            gap: 16,
          }}
        >
          <Stub
            title="Ближе всего к срабатыванию"
            step={9}
            what="Правило с полосой прогресса: видно, что подходишь к границе, до того как её пересечёшь."
          />
          <Stub
            title="Инциденты сегодня"
            step={10}
            what="Срабатывания правил с отметкой «соблюдено» или «нарушено»."
          />
          <div className="card" style={{ padding: '14px 18px' }}>
            <div style={{ display: 'flex', alignItems: 'baseline', marginBottom: 8 }}>
              <span className="klabel">Последние сделки</span>
              <button
                onClick={() => navigate('/trades')}
                style={{
                  marginLeft: 'auto',
                  border: 'none',
                  background: 'transparent',
                  color: 'var(--accent)',
                  fontSize: 12,
                  padding: 0,
                }}
              >
                все сделки
              </button>
            </div>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
              <thead>
                <tr className="klabel">
                  <th style={{ textAlign: 'left', padding: '4px 0', fontWeight: 400 }}>Время</th>
                  <th style={{ textAlign: 'left', padding: '4px 0', fontWeight: 400 }}>Символ</th>
                  <th style={{ textAlign: 'right', padding: '4px 0', fontWeight: 400 }}>PnL</th>
                  <th style={{ textAlign: 'right', padding: '4px 0', fontWeight: 400 }}>% счёта</th>
                  <th style={{ textAlign: 'right', padding: '4px 0', fontWeight: 400 }}>Разметка</th>
                </tr>
              </thead>
              <tbody>
                {(feed.data?.items ?? []).map((trade) => (
                  <tr key={trade.id} style={{ borderTop: '1px solid #232320' }}>
                    <td className="mono" style={{ padding: '7px 0', color: 'var(--dim)' }}>
                      {time(trade.close_time)}
                    </td>
                    <td style={{ padding: '7px 0' }}>
                      {trade.symbol}
                      <span style={{ color: 'var(--faint)' }}>
                        {' · '}
                        {trade.side === 'long' ? 'long' : 'short'}
                      </span>
                    </td>
                    <td
                      className="mono"
                      style={{ padding: '7px 0', textAlign: 'right', color: pnlColor(trade.profit_usd) }}
                    >
                      {money(trade.profit_usd)}
                    </td>
                    <td
                      className="mono"
                      style={{ padding: '7px 0', textAlign: 'right', color: 'var(--dim)' }}
                    >
                      {pct(trade.account_return_pct)}
                    </td>
                    <td
                      style={{
                        padding: '7px 0',
                        textAlign: 'right',
                        color:
                          trade.marking === 'unreviewed'
                            ? 'var(--warn)'
                            : trade.marking === 'violation'
                              ? 'var(--bad)'
                              : 'var(--faint)',
                      }}
                    >
                      {trade.marking === 'unreviewed'
                        ? 'не размечено'
                        : trade.tags.map((t) => t.name).join(', ') || 'по системе'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {(feed.data?.items ?? []).length === 0 && (
              <div className="hint" style={{ marginTop: 8 }}>
                Сегодня сделок нет.
              </div>
            )}
          </div>
        </div>

        <div
          style={{
            flexGrow: 1,
            minWidth: 280,
            display: 'flex',
            flexDirection: 'column',
            gap: 16,
          }}
        >
          <Stub
            title="Дисциплина"
            step={7}
            what="Дни подряд без нарушений, лучший результат и заморозки."
          />
          <EntryEditor level="day" periodStart={today.day} entry={today.entry} />
        </div>
      </div>
    </div>
  )
}

function Tiles({ today, empty }: { today: Today; empty?: boolean }) {
  const c = today.counters
  const dash = (value: string) => (empty ? '—' : value)
  return (
    <div style={{ display: 'flex', gap: 28, flexWrap: 'wrap', marginBottom: 12 }}>
      <Tile
        label="PnL дня"
        value={dash(money(c.profit_usd))}
        sub={dash(pct(c.equity_pct))}
        color={empty ? undefined : pnlColor(c.profit_usd)}
      />
      <Tile
        label="Просадка от пика"
        value={dash(`${Number(c.drawdown_pct).toFixed(2)}%`)}
        sub={dash(`пик ${pct(c.peak_pct)}`)}
        color={!empty && Number(c.drawdown_pct) > 0 ? 'var(--warn)' : undefined}
      />
      <Tile
        label="Сделок"
        value={String(c.all_trades)}
        sub={c.unmarked > 0 ? `${c.unmarked} не размечено` : 'все размечены'}
      />
      <Tile
        label="Нарушений"
        value={String(c.violations)}
        sub={dash(`покрытие ${Number(c.coverage_pct).toFixed(0)}%`)}
        color={!empty && c.violations > 0 ? 'var(--bad)' : undefined}
      />
      <Tile
        label="Убытков подряд"
        value={String(c.loss_streak)}
        sub={c.loss_streak > 0 ? `суммарно ${pct(c.loss_sum_pct)}` : 'серии нет'}
        color={!empty && c.loss_streak > 1 ? 'var(--bad)' : undefined}
      />
      <Tile
        label="Цена эмоций"
        value={dash(money(c.emotion_cost_usd))}
        sub="за день"
        color={!empty && Number(c.emotion_cost_usd) < 0 ? 'var(--bad)' : undefined}
      />
    </div>
  )
}

function Tile({
  label,
  value,
  sub,
  color,
}: {
  label: string
  value: string
  sub: string
  color?: string
}) {
  return (
    <div>
      <div className="klabel">{label}</div>
      <div className="mono" style={{ fontSize: 21, marginTop: 3, color: color ?? 'var(--fg)' }}>
        {value}
      </div>
      <div className="mono hint">{sub}</div>
    </div>
  )
}

const MONTHS = [
  'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
  'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря',
]

function humanDay(value: string): string {
  const [, m, d] = value.split('-').map(Number)
  return `${d} ${MONTHS[m - 1]}`
}

function Centered({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        minHeight: 'calc(100vh - 160px)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
      }}
    >
      {children}
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
