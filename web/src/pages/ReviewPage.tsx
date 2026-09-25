import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { ApiError, api } from '../lib/api'
import type { DiaryList, ReviewState } from '../lib/types'
import { money, pct } from '../ui/format'

// Э-07 по прототипу: факты дня слева, четыре пронумерованных поля справа.
// Разбор идёт поверх цифр, а не вместо них — поэтому цифры на экране, а не
// «вспомни, как прошёл день».
const PLAN = [
  { key: 'yes', label: 'Да' },
  { key: 'partial', label: 'Частично' },
  { key: 'no', label: 'Нет' },
] as const

const MONTHS = [
  'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
  'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря',
]

function humanDate(value: string): string {
  const [y, m, d] = value.split('-').map(Number)
  return `${d} ${MONTHS[m - 1]} ${y}`
}

export function ReviewPage() {
  const { day = '' } = useParams()
  const navigate = useNavigate()
  const qc = useQueryClient()

  const state = useQuery<ReviewState>({
    queryKey: ['review', day],
    queryFn: () => api.get<ReviewState>(`/reviews/${day}`),
    enabled: Boolean(day),
  })
  const facts = useQuery<DiaryList>({
    queryKey: ['diary', 'day-one', day],
    queryFn: () => api.get<DiaryList>(`/entries?level=day&from=${day}&to=${day}`),
    enabled: Boolean(day),
  })

  const [plan, setPlan] = useState<'yes' | 'partial' | 'no'>('partial')
  const [pull, setPull] = useState('')
  const [score, setScore] = useState<number | null>(null)
  const [takeaway, setTakeaway] = useState('')
  const [error, setError] = useState('')

  const send = useMutation({
    mutationFn: () =>
      api.post('/reviews', {
        day,
        plan_followed: plan,
        pull_text: pull || null,
        execution_score: score,
        takeaway: takeaway || null,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['today'] })
      qc.invalidateQueries({ queryKey: ['review', day] })
      qc.invalidateQueries({ queryKey: ['diary'] })
      navigate('/today')
    },
    onError: (err) =>
      setError(err instanceof ApiError ? err.message : 'Не получилось отправить.'),
  })

  const done = state.data?.review
  const f = facts.data?.items[0]?.facts

  return (
    <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap' }}>
      <div style={{ width: 300, flexShrink: 0 }}>
        <div className="klabel" style={{ marginBottom: 14 }}>
          Факты дня
        </div>
        <div className="card" style={{ padding: '18px 20px' }}>
          {f ? (
            <>
              <Fact
                label="PnL"
                value={money(f.profit_usd)}
                color={Number(f.profit_usd) < 0 ? 'var(--bad)' : 'var(--ok)'}
                first
              />
              <Fact label="От депозита" value={pct(f.account_return_pct)} />
              <Fact label="Сделок" value={String(f.trades)} />
              <Fact
                label="Нарушений"
                value={String(f.violations)}
                color={f.violations > 0 ? 'var(--bad)' : undefined}
              />
              {/* Подпись «шаг 9» стояла здесь до шага 11 — уже после того,
                  как шаг 9 прошёл и число стало настоящим. Заглушка не гаснет
                  сама, когда шаг закрывается: её надо снимать руками. */}
              <Fact label="Сработало правил" value={String(f.rules_fired)} />
              <Fact
                label="Покрытие разметкой"
                value={`${Number(f.coverage_pct).toFixed(0)}%`}
              />
              <Fact
                label="Цена эмоций"
                value={money(f.emotion_cost_usd)}
                color={Number(f.emotion_cost_usd) < 0 ? 'var(--bad)' : undefined}
              />
            </>
          ) : (
            <div className="hint">загрузка…</div>
          )}
        </div>
        <div className="hint" style={{ marginTop: 14 }}>
          Разбор идёт поверх цифр, а не вместо них. Оценивается исполнение,
          не результат.
        </div>
      </div>

      <div style={{ flexGrow: 1, minWidth: 320, maxWidth: 620 }}>
        <div className="serif" style={{ fontSize: 30, marginBottom: 6 }}>
          Как ты торговал {day ? humanDate(day).replace(/ \d{4}$/, '') : ''}
        </div>
        <div className="hint" style={{ marginBottom: 26 }}>
          {done ? 'Разбор заполнен и больше не меняется.' : 'Четыре вопроса, одна минута.'}
        </div>

        {done ? (
          <div className="card" style={{ padding: '18px 20px' }}>
            <Line
              label="План"
              value={PLAN.find((p) => p.key === done.plan_followed)?.label ?? ''}
            />
            <Line label="Что дёрнуло" value={done.pull_text ?? '—'} />
            <Line
              label="Исполнение"
              value={done.execution_score ? `${done.execution_score} из 5` : '—'}
            />
            <Line label="Вывод" value={done.takeaway ?? '—'} />
            <button
              onClick={() => navigate('/today')}
              style={{ marginTop: 14, fontSize: 12, padding: '6px 12px' }}
            >
              К главному экрану
            </button>
          </div>
        ) : (
          <>
            <div style={{ marginBottom: 24 }}>
              <div style={{ fontSize: 13, marginBottom: 10 }}>1. Соблюдал план?</div>
              <div style={{ display: 'flex', gap: 10 }}>
                {PLAN.map((option) => (
                  <button
                    key={option.key}
                    onClick={() => setPlan(option.key)}
                    aria-pressed={plan === option.key}
                    className={plan === option.key ? 'primary' : ''}
                    style={{ fontSize: 13, padding: '10px 16px', borderRadius: 8 }}
                  >
                    {option.label}
                  </button>
                ))}
              </div>
            </div>

            <div style={{ marginBottom: 24 }}>
              <label htmlFor="r-pull" style={{ fontSize: 13 }}>
                2. Что дёрнуло сильнее всего?
              </label>
              <textarea
                id="r-pull"
                value={pull}
                onChange={(e) => setPull(e.target.value)}
                rows={3}
                placeholder="Если ничего — оставь пустым."
                style={{ width: '100%', marginTop: 10, resize: 'vertical' }}
              />
            </div>

            <div style={{ marginBottom: 24 }}>
              <div style={{ fontSize: 13 }}>3. Оценка исполнения</div>
              <div className="hint" style={{ margin: '5px 0 10px' }}>
                Оцени, как ты торговал, не сколько заработал.
              </div>
              <div role="group" aria-label="Оценка исполнения" style={{ display: 'flex', gap: 8 }}>
                {[1, 2, 3, 4, 5].map((value) => (
                  <button
                    key={value}
                    onClick={() => setScore(score === value ? null : value)}
                    aria-pressed={score === value}
                    className={score === value ? 'primary mono' : 'mono'}
                    style={{ width: 44, height: 44, padding: 0, borderRadius: 8, fontSize: 15 }}
                  >
                    {value}
                  </button>
                ))}
              </div>
            </div>

            <div style={{ marginBottom: 30 }}>
              <label htmlFor="r-out" style={{ fontSize: 13 }}>
                4. Один вывод на завтра
              </label>
              <textarea
                id="r-out"
                value={takeaway}
                onChange={(e) => setTakeaway(e.target.value)}
                rows={2}
                placeholder="Одно предложение, которое можно выполнить"
                style={{ width: '100%', marginTop: 10, resize: 'vertical' }}
              />
            </div>

            <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
              <button className="cta" onClick={() => send.mutate()} disabled={send.isPending}>
                {send.isPending ? 'Сохраняю…' : 'Сохранить и закрыть день'}
              </button>
              <span className="hint">Разбор — одно из условий зачёта дня в стрик (ТЗ 7.1).</span>
            </div>
            {error && (
              <div className="err" style={{ marginTop: 14 }}>
                {error}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}

function Fact({
  label,
  value,
  sub,
  color,
  first,
}: {
  label: string
  value: string
  sub?: string
  color?: string
  first?: boolean
}) {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'baseline',
        padding: '7px 0',
        borderTop: first ? undefined : '1px solid #232320',
      }}
    >
      <span style={{ fontSize: 13, color: 'var(--dim)' }}>{label}</span>
      <span
        className="mono"
        style={{ marginLeft: 'auto', fontSize: 17, color: color ?? 'var(--fg)' }}
      >
        {value}
      </span>
      {sub && (
        <span className="mono hint" style={{ marginLeft: 8 }}>
          {sub}
        </span>
      )}
    </div>
  )
}

function Line({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ display: 'flex', gap: 10, marginBottom: 9 }}>
      <span className="hint" style={{ width: 110, flexShrink: 0 }}>
        {label}
      </span>
      <span style={{ fontSize: 13, whiteSpace: 'pre-wrap' }}>{value}</span>
    </div>
  )
}
