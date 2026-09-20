import { NavLink, useNavigate } from 'react-router-dom'
import { api } from '../lib/api'
import { useSetMe } from '../lib/auth'
import type { Me } from '../lib/types'

// Каркас интерфейса из дизайна: сайдбар на семь пунктов и топ-бар.
// «Аналитика» — неактивная плашка: раздел обозначен, но экрана за ним нет.
const SECTIONS = [
  { to: '/today', label: 'Сегодня' },
  { to: '/diary', label: 'Дневник' },
  { to: '/trades', label: 'Сделки' },
  { to: '/incidents', label: 'Инциденты' },
  { to: '/rules', label: 'Правила' },
]

export function Shell({ me, children }: { me: Me; children: React.ReactNode }) {
  const setMe = useSetMe()
  const navigate = useNavigate()

  async function logout() {
    await api.post('/auth/logout')
    setMe(null)
    navigate('/login')
  }

  return (
    <div style={{ display: 'flex', minHeight: '100vh' }}>
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
          <span style={{ fontSize: 13, color: 'var(--dim)' }}>
            Источник не подключён
          </span>
          <div style={{ flexGrow: 1 }} />
          <span style={{ fontSize: 13, color: 'var(--dim)' }}>{me.user.email}</span>
          <button onClick={logout} style={{ fontSize: 12, padding: '6px 12px' }}>
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

        <div style={{ flexGrow: 1, padding: '20px 24px', minHeight: 0 }}>{children}</div>
      </div>
    </div>
  )
}
