import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { api } from '../lib/api'
import type { DiaryItem, DiaryList } from '../lib/types'
import { EntryEditor } from '../ui/EntryEditor'
import { money, pct } from '../ui/format'

// Э-09: переключатель уровня. День — календарь месяца, неделя и месяц —
// карточка периода с метриками ТЗ. Разные вопросы требуют разной формы:
// «что было в среду» — это календарь, «как прошла неделя» — это цифры.
const LEVELS = [
  { key: 'day', label: 'День' },
  { key: 'week', label: 'Неделя' },
  { key: 'month', label: 'Месяц' },
] as const

type Level = (typeof LEVELS)[number]['key']

const ADMISSION_COLOR: Record<string, string> = {
  green: 'var(--ok)',
  red: 'var(--warn)',
  denied: 'var(--bad)',
}

export function DiaryPage() {
  const [level, setLevel] = useState<Level>('day')
  const [openDay, setOpenDay] = useState<string | null>(null)

  const diary = useQuery<DiaryList>({
    queryKey: ['diary', level],
    queryFn: () => api.get<DiaryList>(`/entries?level=${level}`),
  })

  const items = diary.data?.items ?? []
  const selected = items.find((i) => i.period_start === openDay) ?? null

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16, maxWidth: 1000 }}>
      <div style={{ display: 'flex', gap: 8 }}>
        {LEVELS.map((l) => (
          <button
            key={l.key}
            onClick={() => {
              setLevel(l.key)
              setOpenDay(null)
            }}
            className={level === l.key ? 'primary' : ''}
            style={{ fontSize: 13, padding: '7px 14px' }}
          >
            {l.label}
          </button>
        ))}
      </div>

      {diary.isLoading && <div className="hint">загрузка…</div>}

      {level === 'day' && (
        <>
          <Calendar items={items} openDay={openDay} onOpen={setOpenDay} />
          {selected ? (
            <>
              <DayFactsCard item={selected} />
              <EntryEditor
                level="day"
                periodStart={selected.period_start}
                entry={selected.entry}
              />
            </>
          ) : (
            <div className="hint">Выбери день, чтобы прочитать или записать.</div>
          )}
        </>
      )}

      {level !== 'day' &&
        items.map((item) => (
          <PeriodCard key={item.period_start} item={item} level={level} />
        ))}
    </div>
  )
}

function Calendar({
  items,
  openDay,
  onOpen,
}: {
  items: DiaryItem[]
  openDay: string | null
  onOpen: (day: string) => void
}) {
  if (items.length === 0) return null
  // items приходят от свежего к старому; календарь читается наоборот.
  const days = [...items].reverse()
  const first = new Date(days[0].period_start)
  // Пустые клетки до первого числа, чтобы месяц встал по дням недели.
  const blanks = (first.getDay() + 6) % 7

  return (
    <div className="card" style={{ padding: '16px 18px' }}>
      <div className="klabel" style={{ marginBottom: 12 }}>
        {first.toLocaleDateString('ru-RU', { month: 'long', year: 'numeric' })}
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7, 1fr)', gap: 6 }}>
        {['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'].map((name) => (
          <div key={name} className="hint" style={{ textAlign: 'center' }}>
            {name}
          </div>
        ))}
        {Array.from({ length: blanks }).map((_, i) => (
          <div key={`blank-${i}`} />
        ))}
        {days.map((item) => {
          const day = new Date(item.period_start)
          const open = item.period_start === openDay
          const profit = Number(item.facts.profit_usd)
          return (
            <button
              key={item.period_start}
              onClick={() => onOpen(item.period_start)}
              style={{
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'flex-start',
                gap: 3,
                padding: '7px 8px',
                minHeight: 62,
                borderColor: open ? 'var(--fg)' : 'var(--line-2)',
                background: open ? 'var(--panel-2)' : undefined,
              }}
            >
              <span style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
                <span
                  style={{
                    width: 6,
                    height: 6,
                    borderRadius: 3,
                    background: item.facts.admission
                      ? ADMISSION_COLOR[item.facts.admission]
                      : 'var(--line-2)',
                  }}
                />
                <span className="mono" style={{ fontSize: 12, color: 'var(--dim)' }}>
                  {day.getDate()}
                </span>
                {item.entry && <span className="hint">·</span>}
              </span>
              {item.facts.trades > 0 && (
                <span
                  className="mono"
                  style={{
                    fontSize: 11,
                    color: profit > 0 ? 'var(--ok)' : profit < 0 ? 'var(--bad)' : 'var(--dim)',
                  }}
                >
                  {money(item.facts.profit_usd)}
                </span>
              )}
              {item.facts.violations > 0 && (
                <span style={{ fontSize: 11, color: 'var(--bad)' }}>
                  ! {item.facts.violations}
                </span>
              )}
            </button>
          )
        })}
      </div>
      <div className="hint" style={{ marginTop: 10 }}>
        Точка — допуск дня, «·» — есть запись, «!» — нарушения.
      </div>
    </div>
  )
}

function DayFactsCard({ item }: { item: DiaryItem }) {
  const facts = item.facts
  return (
    <div className="card" style={{ padding: '14px 18px', display: 'flex', gap: 24, flexWrap: 'wrap' }}>
      <Stat
        label="Допуск"
        value={
          facts.admission
            ? `${facts.admission === 'green' ? 'зелёный' : facts.admission === 'red' ? 'под риском' : 'нет'}${
                facts.check_score !== null ? ` (${facts.check_score})` : ''
              }`
            : 'чека не было'
        }
      />
      <Stat label="Сделок" value={String(facts.trades)} />
      <Stat label="Нарушений" value={String(facts.violations)} />
      <Stat label="Без разметки" value={String(facts.unmarked)} />
      <Stat label="Результат" value={money(facts.profit_usd)} />
      <Stat label="От депозита" value={pct(facts.account_return_pct)} />
      <Stat
        label="Разбор"
        value={
          facts.review_state === 'done'
            ? 'заполнен'
            : facts.review_state === 'pending'
              ? 'не заполнен'
              : '—'
        }
      />
    </div>
  )
}

function PeriodCard({ item, level }: { item: DiaryItem; level: Level }) {
  const f = item.facts
  const from = new Date(item.period_start)
  const to = new Date(item.period_end)
  const title =
    level === 'week'
      ? `Неделя ${from.getDate()}–${to.getDate()} ${to.toLocaleDateString('ru-RU', { month: 'long' })}`
      : from.toLocaleDateString('ru-RU', { month: 'long', year: 'numeric' })

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div className="card" style={{ padding: '18px 20px' }}>
        <div style={{ fontSize: 15, marginBottom: 14 }}>{title}</div>
        {f.trades === 0 ? (
          <div className="hint">Сделок за период нет.</div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 9 }}>
            <Row
              label="Коэффициент дисциплины"
              value={f.discipline_pct === null ? '—' : `${Number(f.discipline_pct).toFixed(0)}%`}
              note={`${f.trades - (f.unmarked ?? 0)} размеченных из ${f.trades}`}
            />
            <Row
              label="Покрытие разметкой"
              value={`${Number(f.coverage_pct ?? 0).toFixed(0)}%`}
              color={Number(f.coverage_pct ?? 0) < 80 ? 'var(--warn)' : undefined}
            />
            <Row
              label="Цена эмоций"
              value={money(f.emotion_cost_usd ?? '0')}
              color={Number(f.emotion_cost_usd ?? 0) < 0 ? 'var(--bad)' : undefined}
            />
            <Row
              label="из них слито"
              value={money(f.lost_on_emotions_usd ?? '0')}
              indent
            />
            <Row
              label="нарушений в плюс"
              value={`${f.violations_profitable ?? 0}  (${money(f.violations_gain_usd ?? '0')})`}
              color={(f.violations_profitable ?? 0) > 0 ? 'var(--warn)' : undefined}
              indent
            />
            <Row
              label="Compliance блокировок"
              value={f.lock_compliance_pct === null ? '— (шаг 9)' : `${f.lock_compliance_pct}%`}
            />
            <Row label="Дней без допуска" value={String(f.days_without_admission ?? 0)} />
            <Row label="Результат" value={money(f.profit_usd)} />
            {f.confidence && !f.confidence.enough_data && (
              <div className="hint" style={{ marginTop: 4 }}>
                Мало данных: {f.confidence.days_available} торговых дней из{' '}
                {f.confidence.days_required}. Выводы делать рано.
              </div>
            )}
          </div>
        )}
      </div>
      <EntryEditor level={level} periodStart={item.period_start} entry={item.entry} />
    </div>
  )
}

function Row({
  label,
  value,
  note,
  color,
  indent,
}: {
  label: string
  value: string
  note?: string
  color?: string
  indent?: boolean
}) {
  return (
    <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
      <span
        className="hint"
        style={{ width: 210, paddingLeft: indent ? 16 : 0, flexShrink: 0 }}
      >
        {label}
      </span>
      <span className="mono" style={{ fontSize: 13, color: color ?? 'var(--fg)' }}>
        {value}
      </span>
      {note && <span className="hint">{note}</span>}
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="klabel" style={{ marginBottom: 4 }}>
        {label}
      </div>
      <div className="mono" style={{ fontSize: 13 }}>
        {value}
      </div>
    </div>
  )
}
