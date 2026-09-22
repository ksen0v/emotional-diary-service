import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { ApiError, api } from '../lib/api'
import type { DiaryList, ReviewState } from '../lib/types'
import { money } from '../ui/format'

// Э-07: четыре поля, ни одного необязательного по смыслу. Сверху — факты дня
// без комментариев: разбор идёт поверх цифр, не вместо них.
const PLAN = [
  { key: 'yes', label: 'Да' },
  { key: 'partial', label: 'Частично' },
  { key: 'no', label: 'Нет' },
] as const

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
    queryKey: ['diary', 'day', day],
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
  const dayFacts = facts.data?.items[0]?.facts

  return (
    <div style={{ maxWidth: 620, display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div>
        <div className="serif" style={{ fontSize: 22 }}>
          Разбор за{' '}
          {day
            ? new Date(day).toLocaleDateString('ru-RU', { day: 'numeric', month: 'long' })
            : ''}
        </div>
        <div className="hint" style={{ marginTop: 4 }}>
          Оцени, как ты торговал, не сколько заработал.
        </div>
      </div>

      {dayFacts && (
        <div
          className="card"
          style={{ padding: '14px 18px', display: 'flex', gap: 26, flexWrap: 'wrap' }}
        >
          <Fact label="Сделок" value={String(dayFacts.trades)} />
          <Fact label="Нарушений" value={String(dayFacts.violations)} />
          <Fact label="Результат" value={money(dayFacts.profit_usd)} />
          <Fact
            label="Допуск"
            value={
              dayFacts.admission === 'green'
                ? 'зелёный'
                : dayFacts.admission === 'red'
                  ? 'под риском'
                  : dayFacts.admission === 'denied'
                    ? 'нет'
                    : 'чека не было'
            }
          />
        </div>
      )}

      {done ? (
        <div className="card" style={{ padding: '18px 20px' }}>
          <div className="klabel" style={{ marginBottom: 12 }}>
            Разбор заполнен
          </div>
          <Line label="План" value={PLAN.find((p) => p.key === done.plan_followed)?.label ?? ''} />
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
        <div className="card" style={{ padding: '18px 20px' }}>
          <Field label="Соблюдал план?">
            <div style={{ display: 'flex', gap: 6 }}>
              {PLAN.map((option) => (
                <button
                  key={option.key}
                  onClick={() => setPlan(option.key)}
                  className={plan === option.key ? 'primary' : ''}
                  style={{ fontSize: 13, padding: '7px 14px' }}
                >
                  {option.label}
                </button>
              ))}
            </div>
          </Field>

          <Field label="Что дёрнуло сильнее всего?">
            <textarea
              value={pull}
              onChange={(e) => setPull(e.target.value)}
              rows={2}
              placeholder="Если ничего — оставь пустым."
              style={{ width: '100%', resize: 'vertical' }}
            />
          </Field>

          <Field label="Оценка исполнения">
            <div style={{ display: 'flex', gap: 6 }}>
              {[1, 2, 3, 4, 5].map((value) => (
                <button
                  key={value}
                  onClick={() => setScore(score === value ? null : value)}
                  className={score === value ? 'primary' : ''}
                  style={{ width: 40, fontSize: 14, padding: '8px 0' }}
                >
                  {value}
                </button>
              ))}
            </div>
          </Field>

          <Field label="Один вывод на завтра">
            <textarea
              value={takeaway}
              onChange={(e) => setTakeaway(e.target.value)}
              rows={2}
              style={{ width: '100%', resize: 'vertical' }}
            />
          </Field>

          <button
            className="primary"
            onClick={() => send.mutate()}
            disabled={send.isPending}
          >
            {send.isPending ? 'Отправляю…' : 'Закрыть разбор'}
          </button>
          {error && (
            <div className="err" style={{ marginTop: 12 }}>
              {error}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 16 }}>
      <div className="klabel" style={{ marginBottom: 8 }}>
        {label}
      </div>
      {children}
    </div>
  )
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="klabel" style={{ marginBottom: 4 }}>
        {label}
      </div>
      <div className="mono" style={{ fontSize: 14 }}>
        {value}
      </div>
    </div>
  )
}

function Line({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ display: 'flex', gap: 10, marginBottom: 8 }}>
      <span className="hint" style={{ width: 110, flexShrink: 0 }}>
        {label}
      </span>
      <span style={{ fontSize: 13, whiteSpace: 'pre-wrap' }}>{value}</span>
    </div>
  )
}
