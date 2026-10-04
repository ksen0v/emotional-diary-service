import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { ApiError, api } from '../lib/api'
import { dateTime } from '../ui/format'

// Админка живёт отдельно от интерфейса трейдера: своя страница, свой вход,
// никакой навигации сервиса. Так и задумано — это разные роли, и смешивать
// их на одном экране значит однажды нажать не ту кнопку.

type AdminUser = {
  id: string
  email: string
  created_at: string
  is_admin: boolean
  trades: number
  incidents: number
  entries: number
  rules: number
}

type Detail = {
  id: string
  email: string
  created_at: string
  is_admin: boolean
  timezone: string | null
  shadow_mode: boolean | null
  source: string | null
  telegram_linked: boolean
  buddy: string | null
  counts: { trades: number; incidents: number; entries: number; rules: number }
}

type LoginOut = { code: string; bot_url: string; expires_at: string }

// Что можно вычистить. Правил в списке нет: это то, что трейдер написал
// про себя, а не история торговли.
const PARTS: { key: string; label: string; note: string }[] = [
  { key: 'trades', label: 'Сделки', note: 'лента и счётчики дня' },
  { key: 'incidents', label: 'Инциденты и блокировки', note: 'вся история срабатываний' },
  { key: 'diary', label: 'Дневник', note: 'записи, чеки, разборы, дни' },
  { key: 'streak', label: 'Стрик', note: 'отметки дней и серия' },
  { key: 'notifications', label: 'Уведомления', note: 'привязка, контакт, тексты, очередь' },
  { key: 'source', label: 'Источник', note: 'подключение, ключи, исполнения' },
]

export function AdminPage() {
  const users = useQuery<{ users: AdminUser[] }>({
    queryKey: ['admin-users'],
    queryFn: () => api.get<{ users: AdminUser[] }>('/admin/users'),
    retry: false,
  })

  // 404 здесь означает «ты не админ» — по ответу не должно быть видно,
  // что такие ручки вообще есть.
  const denied = users.isError

  return (
    <div style={{ padding: '28px 32px', maxWidth: 1100, margin: '0 auto' }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, marginBottom: 20 }}>
        <div style={{ fontSize: 20 }}>Админка</div>
        <div className="hint">служебный экран, не часть интерфейса трейдера</div>
      </div>

      {users.isLoading && <div className="hint">загрузка…</div>}
      {denied && <AdminLogin onDone={() => users.refetch()} />}
      {users.data && <Users rows={users.data.users} />}
    </div>
  )
}

function AdminLogin({ onDone }: { onDone: () => void }) {
  const [login, setLogin] = useState<LoginOut | null>(null)
  const [error, setError] = useState('')
  const [waiting, setWaiting] = useState(false)

  const start = useMutation({
    mutationFn: () => api.post<LoginOut>('/admin/login'),
    onSuccess: (data) => {
      setError('')
      setLogin(data)
      setWaiting(true)
    },
    onError: (err) =>
      setError(err instanceof ApiError ? err.message : 'Не получилось начать вход.'),
  })

  // Опрос вместо потока: вход — единственное место, где страница ещё
  // не представилась, а поток требует сессии. Две секунды — это ровно
  // столько, сколько человек идёт из браузера в Telegram и обратно.
  useEffect(() => {
    if (!waiting || !login) return
    let alive = true
    const timer = setInterval(async () => {
      try {
        const res = await api.get<{ state: string }>(`/admin/login/${login.code}`)
        if (!alive) return
        if (res.state === 'ready') {
          setWaiting(false)
          onDone()
        }
      } catch (err) {
        if (!alive) return
        setWaiting(false)
        setError(err instanceof ApiError ? err.message : 'Вход не прошёл.')
      }
    }, 2000)
    return () => {
      alive = false
      clearInterval(timer)
    }
  }, [waiting, login, onDone])

  return (
    <div className="card" style={{ padding: '20px 22px', maxWidth: 560 }}>
      <div className="klabel" style={{ marginBottom: 12 }}>
        Вход
      </div>

      {!login && (
        <>
          <div className="hint" style={{ marginBottom: 14, lineHeight: 1.6 }}>
            Пароля у админки нет. Нажми кнопку, открой ссылку в Telegram — бот
            узнает тебя по чату, и страница откроется сама.
          </div>
          <button
            className="primary"
            onClick={() => start.mutate()}
            disabled={start.isPending}
            style={{ fontSize: 13, padding: '8px 16px' }}
          >
            {start.isPending ? 'Готовлю ссылку…' : 'Войти через Telegram'}
          </button>
        </>
      )}

      {login && (
        <>
          <div className="hint" style={{ marginBottom: 12, lineHeight: 1.6 }}>
            Открой ссылку и нажми «Start» в боте. Ссылка одноразовая и живёт
            до {dateTime(login.expires_at)}.
          </div>
          <a
            href={login.bot_url}
            target="_blank"
            rel="noreferrer"
            className="mono"
            style={{ color: 'var(--accent)', wordBreak: 'break-all' }}
          >
            {login.bot_url}
          </a>
          <div className="hint" style={{ marginTop: 12 }}>
            {waiting ? 'Жду подтверждения из бота…' : 'Ожидание остановлено.'}
          </div>
        </>
      )}

      {error && (
        <div className="err" style={{ marginTop: 12 }}>
          {error}
        </div>
      )}
    </div>
  )
}

function Users({ rows }: { rows: AdminUser[] }) {
  const [open, setOpen] = useState<string | null>(null)
  return (
    <div style={{ display: 'flex', gap: 20, alignItems: 'flex-start' }}>
      <div className="card" style={{ padding: '4px 0', flexGrow: 1, minWidth: 0 }}>
        {rows.length === 0 && (
          <div className="hint" style={{ padding: 16 }}>
            Ни одного пользователя.
          </div>
        )}
        {rows.map((row) => (
          <div
            key={row.id}
            onClick={() => setOpen(row.id === open ? null : row.id)}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 14,
              padding: '10px 18px',
              borderBottom: '1px solid #212120',
              cursor: 'pointer',
              fontSize: 13,
              background: row.id === open ? 'var(--panel-2)' : undefined,
            }}
          >
            <span style={{ flexGrow: 1, minWidth: 0 }}>{row.email}</span>
            {row.is_admin && (
              <span
                style={{
                  fontSize: 11,
                  padding: '3px 9px',
                  borderRadius: 12,
                  border: '1px solid var(--line-2)',
                  color: 'var(--dim)',
                }}
              >
                админ
              </span>
            )}
            <span className="mono hint" style={{ width: 90, textAlign: 'right' }}>
              {row.trades} сд.
            </span>
            <span className="mono hint" style={{ width: 90, textAlign: 'right' }}>
              {row.incidents} инц.
            </span>
            <span className="hint" style={{ width: 150, textAlign: 'right' }}>
              {dateTime(row.created_at)}
            </span>
          </div>
        ))}
      </div>

      {open && <UserCard userId={open} onGone={() => setOpen(null)} />}
    </div>
  )
}

function UserCard({ userId, onGone }: { userId: string; onGone: () => void }) {
  const qc = useQueryClient()
  const [parts, setParts] = useState<string[]>([])
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [error, setError] = useState('')
  const [report, setReport] = useState<string | null>(null)

  const detail = useQuery<Detail>({
    queryKey: ['admin-user', userId],
    queryFn: () => api.get<Detail>(`/admin/users/${userId}`),
  })

  function refresh() {
    qc.invalidateQueries({ queryKey: ['admin-users'] })
    qc.invalidateQueries({ queryKey: ['admin-user', userId] })
  }

  const purge = useMutation({
    mutationFn: () =>
      api.post<{ done: Record<string, number> }>(`/admin/users/${userId}/purge`, {
        parts,
      }),
    onSuccess: (data) => {
      setError('')
      setParts([])
      setReport(
        Object.entries(data.done)
          .map(([key, value]) => `${key}: ${value}`)
          .join(', ') || 'нечего было чистить',
      )
      refresh()
    },
    onError: (err) =>
      setError(err instanceof ApiError ? err.message : 'Не получилось вычистить.'),
  })

  const remove = useMutation({
    mutationFn: () => api.del<{ deleted: string }>(`/admin/users/${userId}`),
    onSuccess: () => {
      refresh()
      onGone()
    },
    onError: (err) =>
      setError(err instanceof ApiError ? err.message : 'Не получилось удалить.'),
  })

  if (!detail.data) {
    return (
      <div className="card" style={{ padding: '18px 20px', width: 420, flexShrink: 0 }}>
        <div className="hint">загрузка…</div>
      </div>
    )
  }

  const d = detail.data
  return (
    <div className="card" style={{ padding: '18px 20px', width: 420, flexShrink: 0 }}>
      <div style={{ marginBottom: 4 }}>{d.email}</div>
      <div className="hint" style={{ marginBottom: 16 }}>
        с {dateTime(d.created_at)}
      </div>

      <Row label="Источник" value={d.source ?? 'не подключён'} />
      <Row label="Таймзона" value={d.timezone ?? '—'} />
      <Row label="Режим наблюдения" value={d.shadow_mode ? 'включён' : 'выключен'} />
      <Row label="Telegram" value={d.telegram_linked ? 'привязан' : 'нет'} />
      <Row label="Доверенное лицо" value={d.buddy ?? 'нет'} />
      <Row
        label="Данные"
        value={
          `${d.counts.trades} сделок · ${d.counts.incidents} инцидентов · `
          + `${d.counts.entries} записей · ${d.counts.rules} правил`
        }
      />

      <div className="klabel" style={{ margin: '18px 0 10px' }}>
        Вычистить
      </div>
      {PARTS.map((part) => (
        <label
          key={part.key}
          style={{ display: 'flex', gap: 10, alignItems: 'baseline', marginBottom: 8 }}
        >
          <input
            type="checkbox"
            checked={parts.includes(part.key)}
            onChange={() =>
              setParts((prev) =>
                prev.includes(part.key)
                  ? prev.filter((k) => k !== part.key)
                  : [...prev, part.key],
              )
            }
          />
          <span style={{ fontSize: 13 }}>
            {part.label} <span className="hint">— {part.note}</span>
          </span>
        </label>
      ))}

      <div className="hint" style={{ margin: '10px 0 12px', lineHeight: 1.6 }}>
        Вычищенное не восстанавливается. Правила трейдера остаются: это то,
        что он написал про себя, а не история торговли.
      </div>

      <button
        onClick={() => purge.mutate()}
        disabled={parts.length === 0 || purge.isPending}
        style={{ fontSize: 12, padding: '6px 12px' }}
      >
        {purge.isPending ? 'Чищу…' : 'Вычистить выбранное'}
      </button>

      {report && (
        <div className="hint mono" style={{ marginTop: 10 }}>
          {report}
        </div>
      )}

      <div
        style={{ marginTop: 20, paddingTop: 16, borderTop: '1px solid var(--line)' }}
      >
        {!confirmDelete ? (
          <button
            onClick={() => setConfirmDelete(true)}
            disabled={d.is_admin}
            style={{ fontSize: 12, padding: '6px 12px' }}
          >
            Удалить пользователя
          </button>
        ) : (
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <button
              className="primary"
              onClick={() => remove.mutate()}
              disabled={remove.isPending}
              style={{ fontSize: 12, padding: '6px 12px' }}
            >
              Удалить насовсем
            </button>
            <button
              onClick={() => setConfirmDelete(false)}
              style={{ fontSize: 12, padding: '6px 12px' }}
            >
              Отмена
            </button>
          </div>
        )}
        <div className="hint" style={{ marginTop: 8 }}>
          {d.is_admin
            ? 'Админа удалить нельзя: это потерять вход в эту же панель.'
            : 'Уйдёт всё: сделки, инциденты, дневник, стрик, подключения и сам аккаунт.'}
        </div>
      </div>

      {error && (
        <div className="err" style={{ marginTop: 12 }}>
          {error}
        </div>
      )}
    </div>
  )
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ display: 'flex', gap: 12, marginBottom: 8, fontSize: 13 }}>
      <span className="hint" style={{ width: 150, flexShrink: 0 }}>
        {label}
      </span>
      <span style={{ minWidth: 0 }}>{value}</span>
    </div>
  )
}
