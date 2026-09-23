import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ApiError, api } from '../lib/api'
import type { Streak } from '../lib/types'
import { plural } from './format'

function isFrozen(streak: Streak, day: string): boolean {
  return streak.days.some((d) => d.day === day && d.reason === 'frozen')
}

// Заморозка дня (ТЗ 7.2): день не рвёт серию и не удлиняет её. В прототипе
// такого управления нет — в нём вообще нет экрана про заморозку, — но без
// кнопки правило остаётся только в API, а применить его не к чему.
export function FreezeDay({
  streak,
  day,
  violations,
  compact,
}: {
  streak: Streak
  day: string
  violations: number
  compact?: boolean
}) {
  const queryClient = useQueryClient()
  const frozen = isFrozen(streak, day)

  const freeze = useMutation({
    mutationFn: () => api.post('/streak/freeze', { day }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['today'] })
      queryClient.invalidateQueries({ queryKey: ['diary'] })
    },
  })

  if (frozen) {
    return (
      <div style={{ fontSize: 12, color: 'var(--dim)', marginTop: compact ? 0 : 10 }}>
        День заморожен: серию не рвёт и не удлиняет.
      </div>
    )
  }
  // К дню с нарушением заморозка не применяется (ТЗ 7.2), и сервер её
  // не примет. Предлагать действие, в котором заведомо откажут, хуже,
  // чем не предлагать его вовсе.
  if (violations > 0 || streak.freezes.left <= 0) return null

  return (
    <div style={{ marginTop: compact ? 0 : 10 }}>
      <button
        onClick={() => freeze.mutate()}
        disabled={freeze.isPending}
        title="День без торговли по жизненным причинам: серия не рвётся"
        style={{ fontSize: 12, padding: '5px 11px' }}
      >
        {freeze.isPending ? 'замораживаю…' : 'Заморозить день'}
      </button>
      {compact && (
        <span className="hint" style={{ marginLeft: 10 }}>
          осталось {streak.freezes.left} из {streak.freezes.per_month}
        </span>
      )}
      {freeze.isError && (
        <div style={{ fontSize: 12, color: 'var(--bad)', marginTop: 7 }}>
          {freeze.error instanceof ApiError
            ? freeze.error.message
            : 'Заморозить не получилось.'}
        </div>
      )}
    </div>
  )
}

// Карточка «Дисциплина» с главного экрана прототипа: число, лучший результат,
// заморозки и нейтральная строка. Формулировки нейтральные — это требование
// ТЗ 7.2, а не стиль: пристыживание за оборванный стрик даёт обратный эффект.
export function StreakCard({
  streak,
  today,
  violationsToday,
}: {
  streak: Streak
  today: string
  violationsToday: number
}) {
  return (
    <div className="card" style={{ padding: '14px 18px' }}>
      <div className="klabel" style={{ marginBottom: 8 }}>
        Дисциплина
      </div>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
        <span className="mono" style={{ fontSize: 30 }}>
          {streak.current}
        </span>
        <span style={{ fontSize: 13, color: 'var(--dim)' }}>
          {plural(streak.current, 'день подряд', 'дня подряд', 'дней подряд')}
        </span>
      </div>
      <div style={{ fontSize: 12, color: 'var(--faint)', marginTop: 6 }}>
        Лучший результат {streak.best} · заморозки {streak.freezes.used} из{' '}
        {streak.freezes.per_month}
      </div>
      {/* «Дней без нарушений в этом месяце» показывается отдельно от серии
          нарочно (ТЗ 7.2): серия рвётся одним днём, а месяц — нет. */}
      <div style={{ fontSize: 12, color: 'var(--faint)', marginTop: 3 }}>
        Без нарушений в этом месяце {streak.month.clean} из {streak.month.days}
      </div>
      <div className="serif" style={{ fontSize: 15, color: 'var(--dim)', marginTop: 10 }}>
        В тишине и дисциплине
      </div>
      <FreezeDay streak={streak} day={today} violations={violationsToday} />
    </div>
  )
}
