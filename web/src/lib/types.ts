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

// --- торговый день и чек (Архитектура ч.2 §3.5) ---

export type Admission = {
  verdict: 'green' | 'red' | 'denied'
  score: number | null
  max_score: number
  checked_at: string | null
  weak: { id: string; short: string; points: number; max: number }[]
}

export type DayCounters = {
  all_trades: number
  significant_trades: number
  violations: number
  unmarked: number
  coverage_pct: string
  emotion_cost_usd: string
  loss_streak: number
  equity_pct: string
  peak_pct: string
  drawdown_pct: string
  loss_sum_pct: string
  profit_usd: string
  unrealized_pct: string | null
  drawdown_full_pct: string | null
}

export type TodaySource = {
  provider: string
  sync_state: string
  account: string | null
  last_event_at: string | null
  stale: boolean
  capabilities: Record<string, boolean | string>
}

// Состояние серии дисциплины (ТЗ 7). «days» — полоска за 30 дней: причина
// незачёта приходит с сервера рассчитанной, чтобы на экране и в расчёте
// не оказалось двух разных причин.
export type StreakDay = {
  day: string
  counted: boolean
  reason: string
  text: string
}

export type Streak = {
  current: number
  best: number
  last_day: string | null
  freezes: { used: number; left: number; per_month: number; month: string }
  month: { days: number; clean: number; month: string }
  days: StreakDay[]
  conditions: string[]
}

export type DayState =
  | 'no_source'
  | 'no_check'
  | 'check_failed'
  | 'trading'
  | 'locked'
  | 'session_closed'
  | 'review_pending'

// --- блокировка (шаг 9) ---

// Три состояния, а не два: null — условие снятия выключено, false — включено
// и не выполнено. На чек-листе экрана блокировки это разные строки.
export type LockSatisfied = {
  timer: boolean | null
  review: boolean | null
  buddy: boolean | null
}

export type BreachTrade = { trade_id: string; symbol: string; open_time: string }

// Что показывает красная полоса нарушения. `streak_text` собирает сервер:
// фраза зависит от того, как считается стрик, и собранная на фронте она
// разошлась бы с расчётом.
export type Breach = {
  trades: BreachTrade[]
  first: BreachTrade
  streak_text: string | null
}

export type Lock = {
  id: string
  incident_id: string
  rule_name: string
  rule_text: string
  started_at: string
  timer_until: string | null
  window_until: string
  server_time: string
  requires: { timer: boolean; review: boolean; buddy: boolean }
  satisfied: LockSatisfied
  can_lift: boolean
  state: string
  review_questions: { id: string; text: string; hint: string }[]
  review_filled: boolean
  unlock_short: string
  unlock_text: string
  // Доверенное лицо появится на шаге 13.
  buddy: null
  breach: Breach | null
}

export type LockReviewResult = {
  satisfied: LockSatisfied
  lock_state: string
  lifted: boolean
  message: string
}

// Строка блока «Ближе всего к срабатыванию». Считает сервер целиком, включая
// «2.9% из 5%»: фронт не знает ни приоритета связок, ни того, какое условие
// держит правило.
export type NearRule = {
  rule_id: string
  name: string
  metric: string
  metric_name: string
  value_text: string
  ratio: string
  met: boolean
  hot: boolean
}

// --- инциденты (шаг 10) ---

// Строка ленты. Заголовок, подпись и обе даты приходят собранными: та же
// строка стоит в блоке «Инциденты сегодня» и уйдёт в уведомление, а три
// сборщика опишут одно событие тремя способами.
export type Incident = {
  id: string
  day: string
  date_text: string
  time_text: string
  code: 'rule_fired' | 'violation' | 'lock_breached' | 'no_admission'
  title: string
  detail: string
  // Короткая форма той же строки — для блока «Инциденты сегодня».
  summary: string
  // Исходов у инцидента два — соблюдено и нарушено (ТЗ 9.2). Третье значение
  // не исход, а то, что он ещё идёт.
  outcome: 'open' | 'kept' | 'breached'
  outcome_text: string
  shadow: boolean
  opened_at: string
  closed_at: string | null
  rule: { id: string | null; name: string | null; text_at_firing: string | null }
  lock: {
    id: string
    started_at: string
    timer_until: string | null
    window_until: string
    state: string
    lifted_at: string | null
    lift_reason: string | null
  } | null
}

export type IncidentsList = {
  items: Incident[]
  next_cursor: string | null
  has_more: boolean
  totals: {
    count: number
    kept: number
    breached: number
    open: number
    // Доля соблюдённых среди закончившихся. null — закончившихся ещё нет.
    discipline_pct: number | null
  }
  period: { label: string; from: string; to: string }
}

export type Today = {
  day: string
  server_time: string
  day_ends_at: string
  state: DayState
  shadow_mode: boolean
  admission: Admission | null
  session: { opened_at: string | null; closed_at: string | null }
  lock: Lock | null
  near_rules: NearRule[]
  // Блок «Инциденты сегодня» из прототипа Main.dc.html.
  incidents: Incident[]
  streak: Streak
  entry: DiaryEntry | null
  review: {
    state: string
    pending_day: string | null
    required_for_next_session: boolean
  }
  counters: DayCounters
  yesterday: {
    day: string
    trades: number
    violations: number
    profit_usd: string
    admission: 'green' | 'red' | 'denied' | null
    review_state: string
  }
  source: TodaySource | null
  attention: { code: string; count: number; message: string }[]
  thresholds: { pass_score: number; min_score: number }
}

export type PremarketQuestion = {
  id: string
  text: string
  short: string
  min: number
  max: number
  labels: Record<string, string>
  hint: string
  inverted: boolean
}

export type PremarketCatalog = {
  questions: PremarketQuestion[]
  pass_score: number
  min_score: number
  max_score: number
  already_done: boolean
}

export type CheckResult = {
  day: string
  score: number
  max_score: number
  pass_score: number
  min_score: number
  verdict: 'green' | 'red' | 'denied'
  session_opened_at: string | null
  message: string
  restrictions: { text: string; note: string } | null
  weak: { id: string; short: string; points: number; max: number }[]
}

// --- дневник и разбор (Архитектура ч.2 §3.5) ---

export type EntryComment = { id: string; body: string; created_at: string }

export type DiaryEntry = {
  id: string
  level: 'day' | 'week' | 'month'
  period_start: string
  period_end: string
  score: number | null
  status: string | null
  tags: string[]
  body: string | null
  editable_until: string
  editable: boolean
  comments: EntryComment[]
}

// Факты дня для клетки календаря.
export type DayFacts = {
  trades: number
  violations: number
  unmarked: number
  coverage_pct: string
  profit_usd: string
  account_return_pct: string
  emotion_cost_usd: string
  admission: 'green' | 'red' | 'denied' | null
  check_score: number | null
  review_state: string
  rules_fired: number
  locks: number
  locks_kept: number
  counted_in_streak: boolean | null
  streak_reason: string | null
  streak_reason_text: string | null
}

// Факты недели и месяца — метрики из ТЗ 5.1.
export type PeriodFacts = {
  trades: number
  significant_trades: number
  violations: number
  unmarked: number
  profit_usd: string
  account_return_pct: string
  coverage_pct: string
  discipline_pct: string | null
  emotion_cost_usd: string
  lost_on_emotions_usd: string
  violations_profitable: number
  violations_gain_usd: string
  days_without_admission: number
  days_with_trades: number
  tags: { tag: string; days: number }[]
  locks: number
  locks_kept: number
  rules_fired: number
  counted_in_streak: boolean | null
  confidence: { enough_data: boolean; days_available: number; days_required: number }
}

export type DiaryItem = {
  level: 'day' | 'week' | 'month'
  period_start: string
  period_end: string
  entry: DiaryEntry | null
  facts: DayFacts & Partial<PeriodFacts>
}

export type DiaryList = {
  level: 'day' | 'week' | 'month'
  from: string
  to: string
  items: DiaryItem[]
}

export type DiaryPresets = { statuses: string[]; tags: string[] }

export type Review = {
  day: string
  plan_followed: 'yes' | 'partial' | 'no'
  pull_text: string | null
  execution_score: number | null
  takeaway: string | null
  created_at: string
}

export type ReviewState = {
  day: string
  review: Review | null
  state: string
  session_closed_at: string | null
}

// --- правила (шаг 8) ---

export type RuleMetric = {
  key: string
  name: string
  unit: string
  type: 'int' | 'decimal'
  min: string
  max: string
  hint?: string
}

export type RuleMetricBlocked = {
  key: string
  name: string
  unit: string
  requires: string | null
  reason: string
}

export type Named = { key: string; name: string }

export type UnlockCondition = {
  key: 'timer' | 'review' | 'buddy'
  name: string
  params: { minutes?: { min: number; max: number; default: number } }
  requires_contact?: boolean
}

export type RulesDictionary = {
  metrics: RuleMetric[]
  unavailable_metrics: RuleMetricBlocked[]
  comparators: Named[]
  connectors: Named[]
  significance_pct: string
  max_conditions: number
  unlock_conditions: UnlockCondition[]
  buddy_available: boolean
  buddy_note: string
}

export type RuleCondition = {
  metric: string
  cmp: string
  value: number | string
  conn?: 'and' | 'or'
}

export type RuleActions = {
  alert: boolean
  lock: { enabled: boolean; minutes: number | null }
  buddy: boolean
  remind_after_minutes?: number
}

export type RuleUnlock = { timer: boolean; review: boolean; buddy: boolean }

export type Rule = {
  id: string
  name: string
  kind: 'system' | 'user'
  system_code: string | null
  enabled: boolean
  // У системного правила условий нет: они в коде обработчика, а не в базе.
  conditions: { items: RuleCondition[] } | null
  actions: RuleActions
  unlock: RuleUnlock
  if_text: string
  human_text: string
  summary: string
  fired_last_30d: number
  version: number
  updated_at: string
  editable_fields: string[] | null
  // Что у системного триггера ещё не работает. null — работает целиком.
  pending: { short: string; text: string } | null
}

// У каждого «ещё не готово» свой флаг и свой текст. Общего «движок готов»
// здесь нет: на шаге 9 именно он унёс с экрана предупреждение о системных
// триггерах, когда движок включился, а триггеры — нет.
export type RulesEngine = {
  active: boolean
  system_active: boolean
  retro_active: boolean
  shadow_mode: boolean
  note: string
  system_note: string
  shadow_note: string
}

export type RulesList = {
  rules: Rule[]
  unavailable_metrics: string[]
  engine: RulesEngine
}

export type RulePreview = {
  if_text: string
  human_text: string
  summary: string
  valid: boolean
  problem: { code: string; message: string; details?: Record<string, unknown> } | null
}
