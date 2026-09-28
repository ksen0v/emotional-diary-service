import { NavLink, useNavigate } from 'react-router-dom'
import { api } from '../lib/api'
import { useSetMe } from '../lib/auth'
import { useToday } from '../pages/TodayPage'
import type { Me, Today } from '../lib/types'
import { LockedScreen } from './LockedScreen'
import { LiveProvider, useLive } from '../lib/stream'
import { ago, dateTime, plural } from './format'

// Каркас интерфейса из дизайна: сайдбар на семь пунктов и топ-бар.
// «Аналитика» — неактивная плашка: раздел обозначен, но экрана за ним нет.
const SECTIONS: { to: string; label: string; badge?: 'unmarked' | 'incidents' }[] = [
  { to: '/today', label: 'Сегодня' },
  { to: '/diary', label: 'Дневник' },
  { to: '/trades', label: 'Сделки', badge: 'unmarked' },
  { to: '/incidents', label: 'Инциденты', badge: 'incidents' },
  { to: '/rules', label: 'Правила' },
]

export function Shell({ me, children }: { me: Me; children: React.ReactNode }) {
  const setMe = useSetMe()
  const navigate = useNavigate()
  const today = useToday()
  const unmarked =
    today.data?.attention.find((item) => item.code === 'unmarked_trades')?.count ?? 0
  // Бейдж на «Инцидентах» — сегодняшние, как в прототипе. Не «все за месяц»:
  // бейдж отвечает на вопрос «случилось ли что-то прямо сейчас».
  const incidentsToday = today.data?.incidents.length ?? 0

  async function logout() {
    await api.post('/auth/logout')
    setMe(null)
    navigate('/login')
  }

  return (
    // Одно живое соединение на приложение поднимается здесь: топ-бару нужно
    // состояние потока, ленте — числа открытых сделок, и оба должны видеть
    // одно и то же.
    <LiveProvider enabled>
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
            {/* Два бейджа, как в прототипе: неразмеченные сделки и инциденты
                за сегодня. Больше не нужно — их станет много, и они
                перестанут работать. */}
            {s.badge === 'unmarked' && unmarked > 0 && (
              <span className="badge">{unmarked}</span>
            )}
            {s.badge === 'incidents' && incidentsToday > 0 && (
              <span className="badge">{incidentsToday}</span>
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
    </LiveProvider>
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

/**
 * «Синк» в топ-баре: слышим ли мы источник прямо сейчас.
 *
 * Раньше здесь показывалось время последней сверки, и этого было мало. Поток,
 * не поднявшийся ни разу, выглядел точно так же, как рабочий: сверка-то
 * проходила. Первая живая проверка Binance на этом и споткнулась — экран
 * молчал о том, что защиты нет.
 *
 * Обратная ошибка тоже названа: тишина живого потока не повод для тревоги.
 * Трейдер не торгует непрерывно, и «нет событий десять минут» при открытом
 * соединении означает «сделок не было», а не «связь пропала».
 */
function Sync({ today }: { today: Today | undefined }) {
  const source = today?.source
  if (!source) {
    return <span style={{ fontSize: 13, color: 'var(--faint)' }}>Синк нет источника</span>
  }
  const live = useLive()
  const stream = source.stream
  const shown = stream?.expected
    ? stream.connected
      ? { text: 'поток открыт', color: 'var(--dim)' }
      : { text: 'потока нет', color: 'var(--bad)' }
    : source.stale
      ? { text: 'ошибка', color: 'var(--bad)' }
      : {
          text: source.last_event_at ? ago(source.last_event_at) : 'ещё не было',
          color: 'var(--dim)',
        }
  return (
    <span
      style={{ display: 'flex', alignItems: 'center', gap: 7, fontSize: 13 }}
      title={syncTitle(source)}
    >
      <span style={{ color: shown.color }}>
        Синк <span className="mono">{shown.text}</span>
      </span>
      {/* Вкладок больше лимита — эта обновляется опросом. Не ошибка, поэтому
          и сказано спокойно: экран рабочий, просто узнаёт на пару секунд
          позже (Архитектура ч.2 §6, решение 2). */}
      {live.polling && (
        <span
          className="hint"
          title="Живое соединение недоступно: экран обновляется опросом"
        >
          опросом
        </span>
      )}
    </span>
  )
}

function syncTitle(source: NonNullable<Today['source']>): string {
  const lines: string[] = []
  if (source.last_event_at) {
    lines.push(`последняя сверка ${dateTime(source.last_event_at)}`)
  }
  const stream = source.stream
  if (stream?.expected) {
    lines.push(
      stream.connected
        ? `соединение с ${stream.opened_at ? dateTime(stream.opened_at) : 'неизвестно когда'}`
        : stream.last_error || 'соединение не поднято',
    )
    if (stream.last_event_at) {
      lines.push(`последнее событие ${dateTime(stream.last_event_at)}`)
    }
    if (stream.reconnects) lines.push(`переподключений ${stream.reconnects}`)
  }
  return lines.join('\n')
}

