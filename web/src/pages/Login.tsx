import { useState } from 'react'
import { ApiError, api } from '../lib/api'
import { useSetMe } from '../lib/auth'
import type { Me } from '../lib/types'

// Вход и регистрация на одном экране: пока пользователь один,
// отдельная страница регистрации была бы лишним шагом.
export function Login() {
  const setMe = useSetMe()
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const isRegister = mode === 'register'

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setError('')
    setBusy(true)
    try {
      const path = isRegister ? '/auth/register' : '/auth/login'
      const me = await api.post<Me>(path, { email, password })
      setMe(me)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Сервис не отвечает.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: 16,
      }}
    >
      <form className="card" onSubmit={submit} style={{ padding: '26px 28px', width: 380 }}>
        <div className="serif" style={{ fontSize: 26, marginBottom: 4 }}>
          Emotional Diary Service
        </div>
        <div className="hint" style={{ marginBottom: 22 }}>
          {isRegister
            ? 'Заведи аккаунт: дальше подключим дневник и настроим правила.'
            : 'Войди, чтобы продолжить.'}
        </div>

        <label className="klabel" htmlFor="email" style={{ display: 'block', marginBottom: 6 }}>
          Почта
        </label>
        <input
          id="email"
          type="email"
          autoComplete="username"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          style={{ width: '100%', marginBottom: 14 }}
        />

        <label className="klabel" htmlFor="password" style={{ display: 'block', marginBottom: 6 }}>
          Пароль
        </label>
        <input
          id="password"
          type="password"
          autoComplete={isRegister ? 'new-password' : 'current-password'}
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          style={{ width: '100%' }}
        />
        {isRegister && (
          <div className="hint" style={{ marginTop: 6 }}>
            От 10 символов.
          </div>
        )}

        {error && (
          <div className="err" style={{ marginTop: 14 }} role="alert">
            {error}
          </div>
        )}

        <button
          type="submit"
          className="primary"
          disabled={busy || !email || !password}
          style={{ width: '100%', marginTop: 20 }}
        >
          {busy ? 'Минуту…' : isRegister ? 'Зарегистрироваться' : 'Войти'}
        </button>

        <button
          type="button"
          onClick={() => {
            setMode(isRegister ? 'login' : 'register')
            setError('')
          }}
          style={{ width: '100%', marginTop: 8, border: 'none', fontSize: 13 }}
        >
          {isRegister ? 'У меня уже есть аккаунт' : 'Создать аккаунт'}
        </button>
      </form>
    </div>
  )
}
