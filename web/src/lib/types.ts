// Формы данных повторяют контракты из Архитектуры ч.2 §3.2.
// Когда появится генерация типов из OpenAPI, этот файл заменится ею.

export type Settings = {
  timezone: string
  day_cutoff: string
  pass_score: number
  min_score: number
  significance_pct: string
  active_account_id: string | null
  telegram_enabled: boolean
  shadow_mode: boolean
}

export type Me = {
  user: { id: string; email: string; created_at: string }
  settings: Settings
  modules_disabled: string[]
  onboarding: { source_connected: boolean; tags_mapped: boolean; first_rule: boolean }
}

export type SessionRow = {
  id: string
  created_at: string
  last_seen_at: string
  user_agent: string | null
  ip: string | null
  current: boolean
}

export type SettingsPatched = { settings: Settings; notice: string | null }
