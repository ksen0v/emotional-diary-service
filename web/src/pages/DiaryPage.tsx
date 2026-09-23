import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { api } from '../lib/api'
import type { DiaryItem, DiaryList } from '../lib/types'
import { Comments, TagChip } from '../ui/EntryEditor'
import { useEntryDraft, usePresets } from '../ui/entry'
import { money, moneyShort, pct, plural } from '../ui/format'
import { useToday } from './TodayPage'

// Э-09 по прототипу: календарь — постоянная левая колонка на всех уровнях,
// переключатель меняет только правую панель. Календарь не исчезает потому,
// что он и есть способ навигации: клик по клетке выбирает период любого уровня.
const LEVELS = [
  { key: 'day', label: 'День' },
  { key: 'week', label: 'Неделя' },
  { key: 'month', label: 'Месяц' },
] as const

type Level = (typeof LEVELS)[number]['key']

const WEEKDAYS = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс']
const MONTHS = [
  'Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
  'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь',
]
const MONTHS_SHORT = [
  'янв', 'фев', 'мар', 'апр', 'мая', 'июн',
  'июл', 'авг', 'сен', 'окт', 'ноя', 'дек',
]
const MONTHS_GENITIVE = [
  'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
  'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря',
]
const WEEKDAYS_LONG = [
  'понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье',
]

// Цвет точки в клетке. Порядок проверок — это и есть смысл легенды:
// «без допуска» важнее нарушения, потому что торговля без допуска сама
// по себе инцидент, а нарушение внутри дня с допуском — часть работы.
const DOT_LEGEND = [
  { color: 'var(--ok)', label: 'зелёный' },
  { color: 'var(--warn)', label: 'под риском' },
  { color: 'var(--bad)', label: 'нарушение' },
  { color: 'var(--violet)', label: 'без допуска' },
  { color: '#4c4a44', label: 'вне рынка' },
]

function dotColor(facts: DiaryItem['facts'] | undefined): string {
  if (!facts) return 'transparent'
  const traded = facts.trades > 0
  if (!traded && facts.admission === null) return '#4c4a44'
  if (facts.admission === 'denied' || (facts.admission === null && traded)) {
    return 'var(--violet)'
  }
  if (facts.violations > 0) return 'var(--bad)'
  if (facts.admission === 'red') return 'var(--warn)'
  return 'var(--ok)'
}

function iso(d: Date): string {
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${m}-${day}`
}
function parse(value: string): Date {
  const [y, m, d] = value.split('-').map(Number)
  return new Date(y, m - 1, d)
}
function addDays(value: string, days: number): string {
  const d = parse(value)
  d.setDate(d.getDate() + days)
  return iso(d)
}
function addMonths(value: string, months: number): string {
  const d = parse(value)
  return iso(new Date(d.getFullYear(), d.getMonth() + months, 1))
}
function startOfWeek(value: string): string {
  const d = parse(value)
  return addDays(value, -((d.getDay() + 6) % 7))
}
function startOfMonth(value: string): string {
  const d = parse(value)
  return iso(new Date(d.getFullYear(), d.getMonth(), 1))
}
function endOfMonth(value: string): string {
  const d = parse(value)
  return iso(new Date(d.getFullYear(), d.getMonth() + 1, 0))
}
function shortDate(value: string): string {
  const d = parse(value)
  return `${d.getDate()} ${MONTHS_SHORT[d.getMonth()]}`
}
function longDate(value: string): string {
  const d = parse(value)
  const month = MONTHS_GENITIVE[d.getMonth()]
  return `${d.getDate()} ${month}, ${WEEKDAYS_LONG[(d.getDay() + 6) % 7]}`
}

export function DiaryPage() {
  const today = useToday()
  const todayIso = today.data?.day ?? iso(new Date())

  const [level, setLevel] = useState<Level>('day')
  const [sel, setSel] = useState<string | null>(null)
  const selected = sel ?? todayIso
  const month = startOfMonth(selected)

  // Ключ календаря и ключ правой панели обязаны различаться даже на уровне
  // «день», когда обе смотрят на один месяц: react-query держит один кэш на
  // ключ, и при совпадении две разные выборки перетирают друг друга —
  // календарь получал ответ на запрос одного дня и оставался пустым.
  const calendar = useQuery<DiaryList>({
    queryKey: ['diary', 'calendar', month],
    queryFn: () =>
      api.get<DiaryList>(
        `/entries?level=day&from=${month}&to=${endOfMonth(month)}`,
      ),
    enabled: Boolean(today.data),
  })

  const weekStart = startOfWeek(selected)
  const periodStart = level === 'week' ? weekStart : month
  const period = useQuery<DiaryList>({
    queryKey: ['diary', 'period', level, periodStart],
    queryFn: () =>
      api.get<DiaryList>(
        `/entries?level=${level}&from=${periodStart}&to=${periodStart}`,
      ),
    enabled: level !== 'day' && Boolean(today.data),
  })

  const byDay = new Map((calendar.data?.items ?? []).map((i) => [i.period_start, i]))
  const dayItem = byDay.get(selected) ?? null
  const periodItem = level === 'day' ? dayItem : (period.data?.items[0] ?? null)

  // ——— навигация ———
  function step(direction: 1 | -1) {
    if (level === 'month') {
      const next = addMonths(month, direction)
      setSel(direction < 0 ? next : minDate(endOfMonth(next), todayIso))
      return
    }
    const days = level === 'week' ? 7 : 1
    const next = addDays(selected, direction * days)
    setSel(direction > 0 ? minDate(next, todayIso) : next)
  }
  const atEnd =
    level === 'month'
      ? month === startOfMonth(todayIso)
      : level === 'week'
        ? weekStart === startOfWeek(todayIso)
        : selected === todayIso

  const navLabel =
    level === 'day'
      ? shortDate(selected)
      : level === 'week'
        ? `${shortDate(weekStart)} — ${shortDate(addDays(weekStart, 6))}`
        : `${MONTHS[parse(month).getMonth()]} ${parse(month).getFullYear()}`

  function inSelection(day: string): boolean {
    if (level === 'day') return day === selected
    if (level === 'week') return day >= weekStart && day <= addDays(weekStart, 6)
    return true
  }

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 18,
        flexGrow: 1,
        minHeight: 0,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        {LEVELS.map((l) => (
          <button
            key={l.key}
            onClick={() => setLevel(l.key)}
            aria-pressed={level === l.key}
            className={level === l.key ? 'primary' : ''}
            style={{ fontSize: 13, padding: '7px 15px' }}
          >
            {l.label}
          </button>
        ))}
        <div style={{ flexGrow: 1 }} />
        <button
          onClick={() => step(-1)}
          aria-label="Предыдущий период"
          style={{ width: 30, height: 30, padding: 0 }}
        >
          ‹
        </button>
        <span
          className="mono"
          style={{ fontSize: 13, minWidth: 210, textAlign: 'center' }}
        >
          {navLabel}
        </span>
        <button
          onClick={() => step(1)}
          disabled={atEnd}
          aria-label="Следующий период"
          style={{ width: 30, height: 30, padding: 0 }}
        >
          ›
        </button>
        <button
          onClick={() => setSel(todayIso)}
          style={{ marginLeft: 8, fontSize: 13, padding: '7px 13px' }}
        >
          Сегодня
        </button>
      </div>

      <div
        style={{
          display: 'flex',
          gap: 20,
          flexWrap: 'wrap',
          flexGrow: 1,
          minHeight: 460,
          alignItems: 'stretch',
        }}
      >
        <Calendar
          month={month}
          todayIso={todayIso}
          byDay={byDay}
          inSelection={inSelection}
          onPick={(day) => {
            setSel(day)
            if (level === 'month') setLevel('day')
          }}
          hint={
            level === 'day'
              ? 'Клик по клетке открывает запись того дня. Будущие дни недоступны.'
              : level === 'week'
                ? 'Клик по любой клетке выбирает её неделю.'
                : 'Клик по клетке открывает её день.'
          }
        />

        <div
          style={{
            // flexBasis 0 обязателен: с базой auto колонка требует ширину
            // по содержимому, и строка переносится — календарь уезжает вверх,
            // а панель под него.
            flex: '1 1 0',
            minWidth: 320,
            display: 'flex',
            flexDirection: 'column',
            gap: 18,
            minHeight: 0,
          }}
        >
          <PeriodCard
            level={level}
            selected={selected}
            todayIso={todayIso}
            weekStart={weekStart}
            month={month}
            item={periodItem}
            loading={level === 'day' ? calendar.isLoading : period.isLoading}
          />
          {periodItem && (
            <StateCard
              level={level}
              periodStart={periodItem.period_start}
              item={periodItem}
            />
          )}
        </div>
      </div>
    </div>
  )
}

function minDate(a: string, b: string): string {
  return a < b ? a : b
}

function Calendar({
  month,
  todayIso,
  byDay,
  inSelection,
  onPick,
  hint,
}: {
  month: string
  todayIso: string
  byDay: Map<string, DiaryItem>
  inSelection: (day: string) => boolean
  onPick: (day: string) => void
  hint: string
}) {
  const first = parse(month)
  const length = parse(endOfMonth(month)).getDate()
  const blanks = (first.getDay() + 6) % 7

  let profit = 0
  let daysWithTrades = 0
  for (const item of byDay.values()) {
    profit += Number(item.facts.profit_usd)
    if (item.facts.trades > 0) daysWithTrades += 1
  }

  return (
    <div
      className="card"
      style={{
        width: 520,
        flexShrink: 0,
        maxWidth: '100%',
        padding: 18,
        display: 'flex',
        flexDirection: 'column',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'baseline', marginBottom: 12 }}>
        <span style={{ fontSize: 14, fontWeight: 500 }}>
          {MONTHS[first.getMonth()]} {first.getFullYear()}
        </span>
        <span className="mono hint" style={{ marginLeft: 'auto' }}>
          {moneyShort(profit)} · {daysWithTrades}{' '}
          {plural(daysWithTrades, 'день', 'дня', 'дней')}
        </span>
      </div>

      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(7, minmax(0, 1fr))',
          gap: 6,
          marginBottom: 6,
        }}
      >
        {WEEKDAYS.map((name) => (
          <div key={name} className="klabel" style={{ textAlign: 'center' }}>
            {name}
          </div>
        ))}
      </div>

      <div
        style={{ display: 'grid', gridTemplateColumns: 'repeat(7, minmax(0, 1fr))', gap: 6 }}
      >
        {Array.from({ length: blanks }).map((_, i) => (
          <div key={`blank-${i}`} />
        ))}
        {Array.from({ length }, (_, i) => {
          const day = `${month.slice(0, 8)}${String(i + 1).padStart(2, '0')}`
          const item = byDay.get(day)
          const future = day > todayIso
          const on = inSelection(day)
          const value = item ? Number(item.facts.profit_usd) : 0
          return (
            <button
              key={day}
              className="cell"
              onClick={() => onPick(day)}
              disabled={future}
              aria-label={longDate(day)}
              style={{
                background: on ? '#26262f' : '#1f1f1c',
                borderColor: on ? 'var(--accent)' : 'var(--line)',
              }}
            >
              <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <span
                  className="mono"
                  style={{ fontSize: 11, color: future ? '#5c5a54' : 'var(--fg)' }}
                >
                  {i + 1}
                </span>
                <span
                  style={{
                    width: 5,
                    height: 5,
                    borderRadius: 3,
                    background: future ? 'transparent' : dotColor(item?.facts),
                  }}
                />
                {item?.entry && <span className="hint">·</span>}
              </span>
              <span style={{ flexGrow: 1 }} />
              {item && item.facts.trades > 0 && (
                <span
                  className="mono"
                  style={{ fontSize: 11, color: value < 0 ? 'var(--bad)' : 'var(--ok)' }}
                >
                  {moneyShort(item.facts.profit_usd)}
                </span>
              )}
            </button>
          )
        })}
      </div>

      <div style={{ flexGrow: 1, minHeight: 16 }} />

      <div
        style={{
          display: 'flex',
          gap: 14,
          flexWrap: 'wrap',
          paddingTop: 14,
          borderTop: '1px solid #232320',
        }}
      >
        {DOT_LEGEND.map((entry) => (
          <span
            key={entry.label}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              fontSize: 11,
              color: 'var(--faint)',
            }}
          >
            <span
              style={{ width: 5, height: 5, borderRadius: 3, background: entry.color }}
            />
            {entry.label}
          </span>
        ))}
      </div>
      <div className="hint" style={{ marginTop: 12 }}>
        {hint} «·» — есть запись.
      </div>
    </div>
  )
}

type RowOpts = {
  sub?: string
  color?: string
  indent?: boolean
  first?: boolean
  labelColor?: string
}

function Row({ label, value, opts = {} }: { label: string; value: string; opts?: RowOpts }) {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'baseline',
        padding: opts.indent ? '4px 0 4px 16px' : '9px 0',
        borderTop:
          opts.first || opts.indent ? '1px solid transparent' : '1px solid #232320',
      }}
    >
      <span
        style={{
          fontSize: opts.indent ? 12 : 13,
          color: opts.labelColor ?? (opts.indent ? 'var(--faint)' : 'var(--dim)'),
        }}
      >
        {label}
      </span>
      <span
        className="mono"
        style={{
          marginLeft: 'auto',
          fontSize: opts.indent ? 13 : 17,
          color: opts.color ?? 'var(--fg)',
        }}
      >
        {value}
      </span>
      {opts.sub && (
        <span className="mono hint" style={{ marginLeft: 9 }}>
          {opts.sub}
        </span>
      )}
    </div>
  )
}

const ADMISSION_WORD: Record<string, [string, string]> = {
  green: ['зелёный', 'var(--ok)'],
  red: ['под риском', 'var(--warn)'],
  denied: ['нет допуска', 'var(--bad)'],
}

function PeriodCard({
  level,
  selected,
  todayIso,
  weekStart,
  month,
  item,
  loading,
}: {
  level: Level
  selected: string
  todayIso: string
  weekStart: string
  month: string
  item: DiaryItem | null
  loading: boolean
}) {
  const title =
    level === 'day'
      ? longDate(selected)
      : level === 'week'
        ? `Неделя ${shortDate(weekStart)} — ${shortDate(addDays(weekStart, 6))}`
        : `${MONTHS[parse(month).getMonth()]} ${parse(month).getFullYear()}`

  const f = item?.facts
  const sub =
    level === 'day'
      ? selected === todayIso
        ? 'сегодня'
        : ''
      : `${f?.days_with_trades ?? 0} ${plural(
          f?.days_with_trades ?? 0,
          'торговый день',
          'торговых дня',
          'торговых дней',
        )}`

  return (
    <div className="card" style={{ padding: '20px 22px' }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 12 }}>
        <span className="serif" style={{ fontSize: 24 }}>
          {title}
        </span>
        <span className="mono hint" style={{ marginLeft: 'auto' }}>
          {sub}
        </span>
      </div>
      <div style={{ marginTop: 14 }}>
        {loading && <div className="hint">загрузка…</div>}
        {!loading && f && level === 'day' && (
          <DayRows facts={f} isToday={selected === todayIso} />
        )}
        {!loading && f && level !== 'day' && <PeriodRows facts={f} />}
      </div>
    </div>
  )
}

// Зачёт дня в серии и причина незачёта. Причина приходит с сервера рассчитанной
// вместе с отметкой (ТЗ 7, Архитектура §3.8): считать её здесь заново значило бы
// держать две копии правила, которые однажды разойдутся.
function StreakRow({
  facts,
  isToday,
}: {
  facts: DiaryItem['facts']
  isToday: boolean
}) {
  // Сегодняшний день не размечается: он ещё не закончился, и назвать его
  // незачтённым до конца дня — неправда.
  if (isToday) {
    return (
      <Row label="Зачёт в серии" value="день идёт" opts={{ color: 'var(--dim)' }} />
    )
  }
  if (facts.counted_in_streak === null) {
    return (
      <Row
        label="Зачёт в серии"
        value="не в счёте"
        opts={{ color: 'var(--dim)', sub: 'серия сохранена' }}
      />
    )
  }
  if (facts.streak_reason === 'frozen') {
    return (
      <Row
        label="Зачёт в серии"
        value="заморожен"
        opts={{ color: 'var(--dim)', sub: 'серия сохранена' }}
      />
    )
  }
  return (
    <Row
      label="Зачёт в серии"
      value={facts.counted_in_streak ? 'зачтён' : 'не зачтён'}
      opts={{
        color: facts.counted_in_streak ? 'var(--ok)' : 'var(--bad)',
        sub: facts.counted_in_streak ? '' : (facts.streak_reason_text ?? ''),
      }}
    />
  )
}

function DayRows({
  facts,
  isToday,
}: {
  facts: DiaryItem['facts']
  isToday: boolean
}) {
  if (facts.trades === 0 && facts.admission === null) {
    return (
      <>
        <Row label="Статус" value="вне рынка" opts={{ first: true, color: 'var(--dim)' }} />
        <Row label="Сделок" value="0" />
        <Row label="Чек" value="не проходился" opts={{ color: 'var(--dim)' }} />
        <StreakRow facts={facts} isToday={isToday} />
      </>
    )
  }
  const [word, color] = facts.admission
    ? ADMISSION_WORD[facts.admission]
    : ['чека не было', 'var(--dim)']
  return (
    <>
      <Row
        label="Допуск"
        value={word}
        opts={{
          first: true,
          color,
          sub: facts.check_score !== null ? `${facts.check_score} из 25` : '',
        }}
      />
      <Row
        label="PnL"
        value={money(facts.profit_usd)}
        opts={{
          color: Number(facts.profit_usd) < 0 ? 'var(--bad)' : 'var(--ok)',
          sub: pct(facts.account_return_pct),
        }}
      />
      <Row
        label="Сделок"
        value={String(facts.trades)}
        opts={{ sub: facts.unmarked > 0 ? `${facts.unmarked} не размечено` : '' }}
      />
      <Row
        label="Нарушений"
        value={String(facts.violations)}
        opts={{ color: facts.violations > 0 ? 'var(--bad)' : undefined }}
      />
      <Row
        label="Покрытие разметкой"
        value={`${Number(facts.coverage_pct).toFixed(0)}%`}
      />
      <Row
        label="Цена эмоций"
        value={money(facts.emotion_cost_usd)}
        opts={{ color: Number(facts.emotion_cost_usd) < 0 ? 'var(--bad)' : undefined }}
      />
      <Row
        label="Блокировок"
        value={String(facts.locks)}
        opts={{ sub: facts.locks > 0 ? `${facts.locks_kept} соблюдено` : 'шаг 9' }}
      />
      <Row
        label="Разбор"
        value={
          facts.review_state === 'done'
            ? 'заполнен'
            : facts.review_state === 'pending'
              ? 'не заполнен'
              : '—'
        }
        opts={{
          color:
            facts.review_state === 'done'
              ? 'var(--ok)'
              : facts.review_state === 'pending'
                ? 'var(--warn)'
                : 'var(--dim)',
        }}
      />
      <StreakRow facts={facts} isToday={isToday} />
    </>
  )
}

function PeriodRows({ facts }: { facts: DiaryItem['facts'] }) {
  const marked = facts.trades - facts.unmarked
  const clean = marked - facts.violations
  const locks = facts.locks ?? 0
  const kept = facts.locks_kept ?? 0
  return (
    <>
      <Row
        label="Коэффициент дисциплины"
        value={facts.discipline_pct === null ? '—' : `${Number(facts.discipline_pct).toFixed(0)}%`}
        opts={{ first: true, sub: marked > 0 ? `${clean} из ${marked}` : 'нечего считать' }}
      />
      <Row
        label="Покрытие разметкой"
        value={`${Number(facts.coverage_pct ?? 0).toFixed(0)}%`}
        opts={{ color: Number(facts.coverage_pct ?? 0) < 80 ? 'var(--warn)' : undefined }}
      />
      <Row
        label="PnL"
        value={money(facts.profit_usd)}
        opts={{ color: Number(facts.profit_usd) < 0 ? 'var(--bad)' : 'var(--ok)' }}
      />
      <Row
        label="Цена эмоций"
        value={money(facts.emotion_cost_usd ?? '0')}
        opts={{ color: Number(facts.emotion_cost_usd ?? 0) < 0 ? 'var(--bad)' : undefined }}
      />
      <Row
        label="из них слито"
        value={money(facts.lost_on_emotions_usd ?? '0')}
        opts={{ indent: true }}
      />
      <Row
        label="нарушений в плюс"
        value={
          (facts.violations_profitable ?? 0) > 0
            ? `${facts.violations_profitable} · ${money(facts.violations_gain_usd ?? '0')}`
            : '0'
        }
        opts={{
          indent: true,
          labelColor: (facts.violations_profitable ?? 0) > 0 ? 'var(--warn)' : undefined,
          color: (facts.violations_profitable ?? 0) > 0 ? 'var(--warn)' : 'var(--dim)',
        }}
      />
      <Row
        label="Compliance блокировок"
        value={locks === 0 ? '100%' : `${Math.round((kept / locks) * 100)}%`}
        opts={{
          color: locks === 0 || kept === locks ? 'var(--ok)' : 'var(--warn)',
          sub: locks > 0 ? `${kept} из ${locks}` : 'блокировок не было',
        }}
      />
      <Row
        label="Дней без допуска"
        value={String(facts.days_without_admission ?? 0)}
        opts={{
          color: (facts.days_without_admission ?? 0) > 0 ? 'var(--violet)' : undefined,
        }}
      />
      {facts.confidence && !facts.confidence.enough_data && (
        <div className="hint" style={{ marginTop: 8 }}>
          Мало данных: {facts.confidence.days_available}{' '}
          {plural(
            facts.confidence.days_available,
            'торговый день',
            'торговых дня',
            'торговых дней',
          )}{' '}
          из {facts.confidence.days_required}. Выводы делать рано.
        </div>
      )}
    </>
  )
}

const STATE_LABEL: Record<Level, string> = {
  day: 'Состояние за день',
  week: 'Состояние за неделю',
  month: 'Состояние за месяц',
}

function StateCard({
  level,
  periodStart,
  item,
}: {
  level: Level
  periodStart: string
  item: DiaryItem
}) {
  const presets = usePresets()
  const draft = useEntryDraft({ level, periodStart, entry: item.entry })
  const [ownTag, setOwnTag] = useState('')

  return (
    <div
      className="card"
      style={{
        padding: '18px 22px',
        flexGrow: 1,
        display: 'flex',
        flexDirection: 'column',
        minHeight: 0,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'baseline', marginBottom: 12 }}>
        <span className="klabel">{STATE_LABEL[level]}</span>
        <span className="mono hint" style={{ marginLeft: 'auto' }}>
          {draft.saveHint}
        </span>
      </div>

      {draft.editable && (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 14,
            flexWrap: 'wrap',
            marginBottom: 14,
          }}
        >
          <span className="hint">Оценка</span>
          <div style={{ display: 'flex', gap: 6 }}>
            {[1, 2, 3, 4, 5].map((value) => (
              <button
                key={value}
                onClick={() => draft.setScore(draft.score === value ? null : value)}
                aria-pressed={draft.score === value}
                className={draft.score === value ? 'primary mono' : 'mono'}
                style={{ width: 32, height: 30, padding: 0, fontSize: 13 }}
              >
                {value}
              </button>
            ))}
          </div>
          <span className="hint">Статус</span>
          <select
            value={draft.status}
            onChange={(e) => draft.setStatus(e.target.value)}
            style={{ fontSize: 13, padding: '6px 9px', maxWidth: 210 }}
            aria-label="Статус периода"
          >
            <option value="">не выбран</option>
            {(presets.data?.statuses ?? []).map((preset) => (
              <option key={preset} value={preset}>
                {preset}
              </option>
            ))}
            {draft.status && !(presets.data?.statuses ?? []).includes(draft.status) && (
              <option value={draft.status}>{draft.status}</option>
            )}
          </select>
        </div>
      )}

      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 14 }}>
        {level === 'day'
          ? Array.from(new Set([...(presets.data?.tags ?? []), ...draft.tags])).map(
              (tag) => (
                <TagChip
                  key={tag}
                  label={tag}
                  on={draft.tags.includes(tag)}
                  onClick={() => draft.toggleTag(tag)}
                  readonly={!draft.editable}
                />
              ),
            )
          : (item.facts.tags ?? []).map((row) => (
              <TagChip
                key={row.tag}
                label={`${row.tag} · ${row.days} ${row.days === 1 ? 'день' : 'дней'}`}
                on={false}
                readonly
              />
            ))}
        {level !== 'day' && (item.facts.tags ?? []).length === 0 && (
          <span className="hint">Ментальных тегов за период не ставилось.</span>
        )}
      </div>

      {level === 'day' && draft.editable && (
        <div style={{ display: 'flex', gap: 6, marginBottom: 14 }}>
          <input
            value={ownTag}
            onChange={(e) => setOwnTag(e.target.value)}
            placeholder="свой тег"
            style={{ maxWidth: 190, fontSize: 13 }}
          />
          <button
            onClick={() => {
              draft.addTag(ownTag)
              setOwnTag('')
            }}
            style={{ fontSize: 12, padding: '6px 12px' }}
          >
            Добавить
          </button>
        </div>
      )}

      {draft.editable ? (
        <textarea
          value={draft.note}
          onChange={(e) => draft.setNote(e.target.value)}
          placeholder="Что происходило с тобой в этот период"
          style={{ width: '100%', flexGrow: 1, minHeight: 92, resize: 'none' }}
        />
      ) : (
        <div style={{ whiteSpace: 'pre-wrap', fontSize: 13 }}>
          {item.entry?.body || <span className="hint">Записи за этот период нет.</span>}
        </div>
      )}

      {draft.error && (
        <div className="err" style={{ marginTop: 10 }}>
          {draft.error}
        </div>
      )}

      {item.entry && (!item.entry.editable || item.entry.comments.length > 0) && (
        <Comments entry={item.entry} />
      )}
    </div>
  )
}
