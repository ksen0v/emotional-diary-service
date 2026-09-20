import { Navigate, Route, Routes } from 'react-router-dom'
import { useMe } from './lib/auth'
import { Login } from './pages/Login'
import { SettingsPage } from './pages/SettingsPage'
import { TradesPage } from './pages/TradesPage'
import { Shell } from './ui/Shell'
import { Stub } from './ui/Stub'

export function App() {
  const me = useMe()

  if (me.isLoading) {
    return (
      <div style={{ padding: 40, color: 'var(--faint)' }}>загрузка…</div>
    )
  }

  if (me.isError) {
    return (
      <div style={{ padding: 40 }}>
        <div className="err">Сервис не отвечает. Проверь, что запущен контейнер api.</div>
      </div>
    )
  }

  if (!me.data) {
    return <Login />
  }

  return (
    <Shell me={me.data}>
      <Routes>
        <Route path="/settings" element={<SettingsPage me={me.data} />} />
        <Route
          path="/today"
          element={
            <Stub
              title="Сегодня"
              step={5}
              what="Допуск, счётчики дня, запись за сегодня и состояние блокировки."
            />
          }
        />
        <Route
          path="/trades"
          element={
<TradesPage />
          }
        />
        <Route
          path="/diary"
          element={<Stub title="Дневник" step={6} what="Записи за день, неделю и месяц." />}
        />
        <Route
          path="/incidents"
          element={
            <Stub
              title="Инциденты"
              step={10}
              what="История срабатываний: соблюдено или нарушено."
            />
          }
        />
        <Route
          path="/rules"
          element={<Stub title="Правила" step={8} what="Конструктор правил и триггеры." />}
        />
        <Route path="*" element={<Navigate to="/trades" replace />} />
      </Routes>
    </Shell>
  )
}
