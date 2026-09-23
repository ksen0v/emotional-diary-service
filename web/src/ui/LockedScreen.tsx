import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { ApiError, api } from '../lib/api'
import type { Lock, LockReviewResult, Today } from '../lib/types'
import { ago, time } from './format'

// Э-13, собран по прототипу Locked.dc.html. Самый важный экран продукта:
// он появляется в худший момент и должен работать против человека, который
// очень хочет его закрыть. Поэтому это перекрытие на всё окно, а не карточка
// внутри раздела: сайдбара здесь нет, как и в прототипе.
//
// Три решения прототипа, которые легко потерять при сборке:
// — таймер крупный и в центре, он единственное, что здесь работает;
// — кнопка снятия видна, но неактивна: трейдер должен видеть, что выход есть
//   и когда он откроется;
// — разбор доступен во время таймера, чтобы писать пока горячо.

const OK = '#EDEBE6'
const RUNNING = '#D9A03F'

export function LockedScreen({ today }: { today: Today }) {
  const lock = today.lock
  if (!lock) return null
  return <Screen lock={lock} today={today} />
}

function Screen({ lock, today }: { lock: Lock; today: Today }) {
  const qc = useQueryClient()
  const left = useCountdown(lock)
  const [answers, setAnswers] = useState({ q1: '', q2: '', q3: '' })
  const [error, setError] = useState('')

  const filled =
    lock.review_filled ||
    lock.review_questions.every((q) => (answers[q.id as 'q1'] ?? '').trim().length >= 10)
  const timeUp = left <= 0
  const timerPending = lock.satisfied.timer === false && !timeUp
  const canPress = !timerPending && (lock.satisfied.review !== false || filled)

  const unlock = useMutation<LockReviewResult | null>({
    mutationFn: async () => {
      if (lock.review_filled || lock.satisfied.review === null) return null
      return api.post<LockReviewResult>(`/locks/${lock.id}/review`, answers)
    },
    onSuccess: (result) => {
      setError(result && !result.lifted ? result.message : '')
      qc.invalidateQueries({ queryKey: ['today'] })
    },
    onError: (err) =>
      setError(err instanceof ApiError ? err.message : 'Не получилось снять блокировку.'),
  })

  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 50,
        background: '#0E0E0D',
        color: OK,
        display: 'flex',
        flexDirection: 'column',
      }}
    >
      {lock.breach && <BreachBanner lock={lock} />}

      <div
        style={{
          flexGrow: 1,
          minHeight: 0,
          padding: '0 24px',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 70,
          flexWrap: 'wrap',
          overflowY: 'auto',
        }}
      >
        <div style={{ width: 400, maxWidth: '100%', textAlign: 'center' }}>
          <div
            className="klabel"
            style={{ marginBottom: 20, color: '#7A7872' }}
          >
            Торговля заблокирована
          </div>
          <div
            className="mono"
            style={{
              fontSize: 92,
              lineHeight: 1,
              letterSpacing: '-2px',
              color: timeUp ? OK : RUNNING,
            }}
          >
            {clock(left)}
          </div>
          <div
            style={{
              marginTop: 26,
              paddingTop: 22,
              borderTop: '1px solid #2B2B27',
              fontSize: 14,
              color: '#DAD7D0',
              lineHeight: 1.6,
            }}
          >
            Правило: {lock.rule_name}
          </div>
          <div className="mono" style={{ fontSize: 12, color: '#7A7872', marginTop: 7 }}>
            Сработало в {time(lock.started_at)} · {lock.unlock_short}
          </div>
        </div>

        <div style={{ width: 500, maxWidth: '100%' }}>
          <div style={{ fontSize: 14, color: '#A9A69E', marginBottom: 18 }}>
            {lock.satisfied.review === null
              ? 'Разбор этим правилом не требуется.'
              : lock.review_filled
                ? 'Разбор заполнен.'
                : 'Чтобы снять блокировку, заполни разбор.'}
          </div>

          {lock.satisfied.review !== null &&
            !lock.review_filled &&
            lock.review_questions.map((q, i) => (
              <div key={q.id}>
                <label htmlFor={`l-${q.id}`} style={{ fontSize: 13, color: '#DAD7D0' }}>
                  {i + 1}. {q.text}
                </label>
                <textarea
                  id={`l-${q.id}`}
                  rows={2}
                  placeholder={q.hint}
                  value={answers[q.id as 'q1'] ?? ''}
                  onChange={(e) =>
                    setAnswers({ ...answers, [q.id]: e.target.value })
                  }
                  style={{
                    width: '100%',
                    display: 'block',
                    margin: '7px 0 16px',
                    background: '#1A1A18',
                    resize: 'none',
                  }}
                />
              </div>
            ))}

          {/* Кнопка видна, но неактивна: скрывать её нельзя — трейдер должен
              видеть, что выход есть и когда он откроется (Дизайн Э-13). */}
          <button
            className={canPress ? 'cta' : undefined}
            disabled={!canPress || unlock.isPending}
            onClick={() => unlock.mutate()}
            style={{
              width: '100%',
              padding: 14,
              marginTop: 4,
              borderRadius: 8,
              fontSize: 14,
              fontWeight: 500,
              ...(canPress
                ? {}
                : { border: '1px solid #2B2B27', background: '#1A1A18', color: '#63615B' }),
            }}
          >
            Снять блокировку
          </button>
          <div
            className="mono"
            style={{ fontSize: 12, color: '#7A7872', marginTop: 9, textAlign: 'center' }}
          >
            {reason(lock, left, filled, timeUp)}
          </div>
          {error && (
            <div className="err" style={{ marginTop: 10, textAlign: 'center' }}>
              {error}
            </div>
          )}
        </div>
      </div>

      <div
        style={{
          flexShrink: 0,
          padding: '14px 24px',
          borderTop: '1px solid #1F1F1C',
          display: 'flex',
          alignItems: 'center',
          gap: 16,
          flexWrap: 'wrap',
        }}
      >
        <span className="mono" style={{ fontSize: 12, color: '#63615B' }}>
          {lock.unlock_text}
        </span>
        <span className="mono" style={{ fontSize: 12, color: '#63615B' }}>
          Синк {today.source?.last_event_at ? ago(today.source.last_event_at) : 'ещё не было'}
        </span>
        <div style={{ flexGrow: 1 }} />
        {/* В прототипе здесь ссылка, и раздел инцидентов теперь есть — но
            перекрытие по дизайну не закрывается, поэтому переход под ним
            ничего бы не открыл. Оставлена надпись, и она говорит, когда
            раздел станет доступен, а не «шаг такой-то». */}
        <span className="mono" style={{ fontSize: 12, color: '#63615B' }}>
          История инцидентов — после снятия блокировки
        </span>
      </div>
    </div>
  )
}

// Красная полоса нарушения. В прототипе у неё три фразы: сделка, сгоревший
// стрик и сигнал Максиму. Первые две здесь есть; третьей нет — доверенное
// лицо появится на шаге 13, и до тех пор про отправленный сигнал сервис
// врать не может.
//
// Фразу про стрик собирает сервер: она зависит от того, как стрик считается
// (отметка ставится завершённому дню), и на фронте она разошлась бы с
// расчётом на следующий же день.
function BreachBanner({ lock }: { lock: Lock }) {
  const first = lock.breach?.first
  if (!first) return null
  const more = (lock.breach?.trades.length ?? 1) - 1
  return (
    <div
      style={{
        flexShrink: 0,
        padding: '13px 24px',
        background: '#3A1E1A',
        borderBottom: '1px solid #5C2A24',
        display: 'flex',
        alignItems: 'center',
        gap: 12,
      }}
    >
      <svg
        width="16"
        height="16"
        viewBox="0 0 24 24"
        fill="none"
        stroke="#E4897A"
        strokeWidth="2.2"
        strokeLinecap="round"
        aria-hidden="true"
      >
        <path d="M12 3 L22 20 H2 Z" />
        <line x1="12" y1="10" x2="12" y2="14" />
        <line x1="12" y1="17" x2="12" y2="17" />
      </svg>
      <span style={{ fontSize: 13, color: '#F0CFC8' }}>
        Открыта сделка {first.symbol} в {time(first.open_time)} во время блокировки.
        {more > 0 && ` И ещё ${more}.`} Инцидент записан как нарушенный.
        {lock.breach?.streak_text ? ` ${lock.breach.streak_text}` : ''}
      </span>
    </div>
  )
}

// Причина, по которой кнопка неактивна, — из прототипа дословно.
function reason(lock: Lock, left: number, filled: boolean, timeUp: boolean): string {
  const waiting = lock.satisfied.timer === false && !timeUp
  const needsReview = lock.satisfied.review === false && !lock.review_filled && !filled

  if (waiting && needsReview) return `неактивно ещё ${clock(left)} · разбор не заполнен`
  if (waiting) return `неактивно ещё ${clock(left)}`
  if (needsReview) return 'заполни все три поля'
  if (lock.satisfied.timer === null && lock.satisfied.review === null) {
    // Ни одного условия снятия: «сегодня я больше не торгую» (ТЗ 6.6).
    return `снять вручную нельзя · кончится в ${time(lock.window_until)}`
  }
  return 'можно снимать'
}

// MM:SS, как в прототипе. Часы появляются только когда их правда больше часа —
// у блокировки до конца дня.
function clock(seconds: number): string {
  const left = Math.max(0, seconds)
  const mm = String(Math.floor((left % 3600) / 60)).padStart(2, '0')
  const ss = String(left % 60).padStart(2, '0')
  const hh = Math.floor(left / 3600)
  return hh > 0 ? `${hh}:${mm}:${ss}` : `${mm}:${ss}`
}

// Таймер считается от серверного времени: при сбитых часах на машине трейдера
// экран иначе отпустил бы его раньше или позже, чем сервер (ч.2 §3.5).
function useCountdown(lock: Lock): number {
  const target = lock.timer_until ?? lock.window_until
  const [skew] = useState(
    () => new Date(lock.server_time).getTime() - Date.now(),
  )
  const compute = () =>
    Math.ceil((new Date(target).getTime() - (Date.now() + skew)) / 1000)
  const [left, setLeft] = useState(compute)

  useEffect(() => {
    setLeft(compute())
    const id = setInterval(() => setLeft(compute()), 1000)
    return () => clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target, skew])

  return left
}
