import { Navigate, Route, Routes } from 'react-router-dom'
import { useMe } from './lib/auth'
import { DiaryPage } from './pages/DiaryPage'
import { Login } from './pages/Login'
import { PremarketPage } from './pages/PremarketPage'
import { ReviewPage } from './pages/ReviewPage'
import { RulesPage } from './pages/RulesPage'
import { SettingsPage } from './pages/SettingsPage'
import { TodayPage } from './pages/TodayPage'
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
        <Route path="/today" element={<TodayPage />} />
        <Route path="/premarket" element={<PremarketPage />} />
        <Route
          path="/trades"
          element={
<TradesPage />
          }
        />
        <Route path="/diary" element={<DiaryPage />} />
        <Route path="/review/:day" element={<ReviewPage />} />
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
        <Route path="/rules" element={<RulesPage />} />
        <Route path="*" element={<Navigate to="/today" replace />} />
      </Routes>
    </Shell>
  )
}
