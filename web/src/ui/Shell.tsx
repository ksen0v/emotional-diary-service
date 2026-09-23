import { NavLink, useNavigate } from 'react-router-dom'
import { api } from '../lib/api'
import { useSetMe } from '../lib/auth'
import { useToday } from '../pages/TodayPage'
import type { Me, Today } from '../lib/types'
import { LockedScreen } from './LockedScreen'
import { ago, dateTime, plural } from './format'

// Каркас интерфейса из дизайна: сайдбар на семь пунктов и топ-бар.
// «Аналитика» — неактивная плашка: раздел обозначен, но экрана за ним нет.
const SECTIONS: { to: string; label: string; badge?: 'unmarked' }[] = [
  { to: '/today', label: 'Сегодня' },
  { to: '/diary', label: 'Дневник' },
  { to: '/trades', label: 'Сделки', badge: 'unmarked' },
  { to: '/incidents', label: 'Инциденты' },
  { to: '/rules', label: 'Правила' },
]

export function Shell({ me, children }: { me: Me; children: React.ReactNode }) {
  const setMe = useSetMe()
  const navigate = useNavigate()
  const today = useToday()
  const unmarked =
    today.data?.attention.find((item) => item.code === 'unmarked_trades')?.count ?? 0

  async function logout() {
    await api.post('/auth/logout')
    setMe(null)
    navigate('/login')
  }

  return (
    <div style={{ display: 'flex', height: '100vh' }}>
      <nav
        style={{
          width: 208,
          flexShrink: 0,
          padding: '18px 12px',
          borderRight: '1px solid var(--line)',
          display: 'flex',
          flexDirection: 'column',
          gap: 2,
          background: '#191917',
        }}
      >
        <div className="serif" style={{ padding: '2px 10px 20px', fontSize: 22 }}>
          Диспетчер
        </div>
        {SECTIONS.map((s) => (
          <NavLink
            key={s.to}
            to={s.to}
            className={({ isActive }) => (isActive ? 'nav on' : 'nav')}
          >
            {s.label}
            {/* Бейдж на «Сделках» — количество неразмеченных. Больше бейджей
                не нужно: их станет много, и они перестанут работать. */}
            {s.badge === 'unmarked' && unmarked > 0 && (
              <span className="badge">{unmarked}</span>
            )}
          </NavLink>
        ))}
        <span className="nav-dim" title="Раздел появится, когда наберётся история">
          Аналитика
        </span>
        <NavLink to="/settings" className={({ isActive }) => (isActive ? 'nav on' : 'nav')}>
          Настройки
        </NavLink>
      </nav>

      <div style={{ flexGrow: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
        <div
          style={{
            height: 48,
            flexShrink: 0,
            padding: '0 24px',
            borderBottom: '1px solid var(--line)',
            display: 'flex',
            alignItems: 'center',
            gap: 22,
            background: '#191917',
          }}
        >
          <Admission today={today.data} />
          <Streak today={today.data} />
          <Sync today={today.data} />
          <div style={{ flexGrow: 1 }} />
          <span style={{ fontSize: 13, color: 'var(--dim)' }}>
            {today.data?.source?.account ?? 'счёт не выбран'}
          </span>
          <button
            onClick={logout}
            title={me.user.email}
            style={{ fontSize: 12, padding: '5px 11px' }}
          >
            Выйти
          </button>
        </div>

        {me.settings.shadow_mode && (
          <div
            style={{
              padding: '8px 24px',
              background: '#201f1c',
              borderBottom: '1px solid var(--line)',
              fontSize: 13,
              color: 'var(--dim)',
            }}
          >
            Режим наблюдения: правила считаются, блокировки не применяются.
          </div>
        )}

        {/* Высота окна — это рабочая область: экраны прототипа растянуты
            по ней, а не обрываются по высоте содержимого. */}
        <div
          style={{
            flexGrow: 1,
            minHeight: 0,
            overflowY: 'auto',
            padding: '20px 24px',
            display: 'flex',
            flexDirection: 'column',
          }}
        >
          {children}
        </div>
      </div>

      {/* Блокировка — состояние приложения, а не экран раздела: перекрытие
          живёт здесь, поэтому оно появится в любом разделе и закроет собой
          навигацию. «Перекрытия не закрываются» — за ними ничего нет, пока
          условие не выполнено (Дизайн §1). */}
      {today.data?.state === 'locked' && <LockedScreen today={today.data} />}
    </div>
  )
}


// Топ-бар из дизайна: допуск, стрик, синк, аккаунт. Все четыре всегда на виду,
// потому что каждый отвечает на вопрос, который иначе задаётся слишком поздно.
const ADMISSION: Record<string, { label: string; color: string }> = {
  green: { label: 'Зелёный', color: 'var(--ok)' },
  red: { label: 'Под риском', color: 'var(--warn)' },
  denied: { label: 'Нет допуска', color: 'var(--bad)' },
}

function Admission({ today }: { today: Today | undefined }) {
  const verdict = today?.admission?.verdict
  const shown = verdict
    ? ADMISSION[verdict]
    : { label: 'Сессия не открыта', color: 'var(--dim)' }
  return (
    <span style={{ display: 'flex', alignItems: 'center', gap: 7, fontSize: 13 }}>
      <span
        style={{ width: 8, height: 8, borderRadius: 4, background: shown.color }}
      />
      <span style={{ color: 'var(--dim)' }}>{shown.label}</span>
    </span>
  )
}

// Стрик в топ-баре: короткая форма, чтобы число дней было на виду в любом
// разделе. Разбор «почему день не зачтён» живёт в дневнике, а не здесь.
function Streak({ today }: { today: Today | undefined }) {
  const streak = today?.streak
  if (!streak) {
    return (
      <span style={{ fontSize: 13, color: 'var(--dim)' }}>
        Стрик <span className="mono" style={{ color: 'var(--faint)' }}>—</span>
      </span>
    )
  }
  return (
    <span
      style={{ fontSize: 13, color: 'var(--dim)' }}
      title={`лучший результат ${streak.best}`}
    >
      Стрик <span className="mono" style={{ color: 'var(--fg)' }}>{streak.current}</span>{' '}
      {plural(streak.current, 'день', 'дня', 'дней')}
    </span>
  )
}

function Sync({ today }: { today: Today | undefined }) {
  const source = today?.source
  if (!source) {
    return <span style={{ fontSize: 13, color: 'var(--faint)' }}>Синк нет источника</span>
  }
  // Если синк умер, защиты нет — это должно быть заметно, поэтому ошибка
  // показывается цветом, а не мелким текстом.
  return (
    <span
      style={{ fontSize: 13, color: source.stale ? 'var(--bad)' : 'var(--dim)' }}
      title={source.last_event_at ? `последняя сверка ${dateTime(source.last_event_at)}` : ''}
    >
      Синк{' '}
      <span className="mono">
        {source.stale
          ? 'ошибка'
          : source.last_event_at
            ? ago(source.last_event_at)
            : 'ещё не было'}
      </span>
    </span>
  )
}

