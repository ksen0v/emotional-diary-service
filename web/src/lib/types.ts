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

export type Tag = { external_id: string; name: string; column_key: string }

export type Trade = {
  id: string
  external_id: string
  source: string
  symbol: string
  side: 'long' | 'short'
  profit_usd: string
  percent: string | null
  account_return_pct: string
  size_usd: string | null
  leverage: string | null
  duration_sec: number | null
  open_time: string
  close_time: string | null
  trading_day: string
  is_significant: boolean
  marking: 'clean' | 'violation' | 'unreviewed'
  marked_by: string
  tags: Tag[]
}

export type Totals = {
  count: number
  significant_count: number
  violations_count: number
  unmarked_count: number
  profit_usd: string
  account_return_pct: string
  coverage_pct: string
}

export type Feed = {
  items: Trade[]
  next_cursor: string | null
  has_more: boolean
  totals: Totals
  period: { level: string; from: string | null; to: string | null; today: string }
}

export type CurvePoint = {
  at: string
  trade_id: string
  equity_pct: string
  peak_pct: string
  drawdown_pct: string
}

export type Curve = {
  day: string
  points: CurvePoint[]
  unrealized: { available: boolean; pct: string | null }
  close: { equity_pct: string; peak_pct: string; max_drawdown_pct: string }
}

export type SourceAccount = {
  id: string
  external_id: string
  name: string
  exchange: string | null
  market: string | null
}

export type ReconcileRow = {
  started_at: string
  finished_at: string | null
  kind: string
  status: string
  trades_seen: number
  trades_new: number
  error: string | null
}

export type RateLimitRow = {
  limit: number | null
  remaining: number | null
  reset_at: string | null
}

export type Connection = {
  id: string
  provider: string
  market: string | null
  key_masked: string | null
  is_active: boolean
  state: string
  ingest_from: string
  activated_at: string | null
  capabilities: Record<string, boolean | string>
  permissions: Record<string, unknown> | null
  last_error: string | null
  accounts: SourceAccount[]
  last_reconcile: ReconcileRow | null
  rate_limit: RateLimitRow | null
}

// Проба подключения: что провайдер показал в ответ. Ничего из этого
// не сохраняется — истории мы не импортируем (ТЗ 4.1).
export type ProbeSample = {
  external_id: string
  symbol: string
  side: 'long' | 'short'
  profit_usd: string
  account_return_pct: string
  duration_sec: number | null
  open_time: string
  close_time: string | null
  tags: string[]
  is_open: boolean
}

export type Probe = {
  accounts: { external_id: string; name: string; exchange: string | null }[]
  entry_tags: { external_id: string; name: string }[]
  trades_seen: number
  sample: ProbeSample[]
  tags_available: boolean
  tags_problem: string | null
  accounts_from_trades: boolean
  window_filter_honored: boolean | null
  mapping_errors: string[]
}

export type ConnectResult = {
  connection: Connection
  accounts: SourceAccount[]
  probe: Probe
  warnings: { code: string; message: string }[]
}

export type SwitchConsequences = {
  consequences?: string[]
  losing_capabilities?: string[]
  gaining_capabilities?: string[]
}

export type Connections = {
  connections: Connection[]
  active_connection_id: string | null
}

export type SyncReport = {
  received: number
  inserted: number
  remarked: number
  unchanged: number
  skipped_before_ingest_from: number
  skipped_open: number
  skipped_unknown_account: number
}

export type TagRow = {
  external_id: string
  name: string
  column_key: string
  is_violation: boolean
}

export type TagsBody = { tags: TagRow[]; available: boolean }

export type MarkingMetrics = {
  period: { level: string; from: string | null; to: string | null; today: string }
  marking: {
    trades: { all: number; significant: number }
    marked: number
    unmarked: number
    coverage_pct: string
    discipline_pct: string | null
    violations: { count: number; profitable: number }
    emotion_cost_usd: string
  }
  confidence: { enough_data: boolean; days_available: number; days_required: number }
}

export type MarkedOut = {
  trade: Trade
  effects: { changed: boolean; marking_before: string | null }
  metrics: MarkingMetrics['marking']
}
