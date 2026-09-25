import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { ApiError, api } from '../lib/api'
import type {
  Curve,
  Feed,
  MarkedOut,
  MarkingMetrics,
  SyncReport,
  Trade,
} from '../lib/types'
import { DayCurve } from '../ui/DayCurve'
import { DevPanel } from '../ui/DevPanel'
import { useConnections } from '../ui/SourceCard'
import { MARKING, duration, money, pct, plural, pnlColor, time } from '../ui/format'

const PERIODS = [
  { key: 'today', label: 'Сегодня' },
  { key: 'week', label: 'Неделя' },
  { key: 'month', label: 'Месяц' },
] as const

const FILTERS = [
  { key: 'all', label: 'Все' },
  { key: 'unmarked', label: 'Без разметки' },
  { key: 'violations', label: 'Нарушения' },
] as const

type Period = (typeof PERIODS)[number]['key']
type Filter = (typeof FILTERS)[number]['key']

export function TradesPage() {
  const connections = useConnections()
  const hasSource = Boolean(
    connections.data?.connections.some((c) => c.is_active),
  )
  const qc = useQueryClient()
  const active = connections.data?.connections.find((c) => c.is_active)
  const provideTags = Boolean(active?.capabilities.provides_tags)
  const isFake = active?.provider === 'fake'
  const [syncReport, setSyncReport] = useState<SyncReport | null>(null)
  const [syncError, setSyncError] = useState('')
  const sync = useMutation({
    mutationFn: () => api.post<SyncReport>('/sync'),
    onSuccess: (data) => {
      setSyncReport(data)
      setSyncError('')
      qc.invalidateQueries({ queryKey: ['trades'] })
      qc.invalidateQueries({ queryKey: ['curve'] })
      qc.invalidateQueries({ queryKey: ['marking-metrics'] })
      qc.invalidateQueries({ queryKey: ['connections'] })
    },
    onError: (err) => {
      setSyncReport(null)
      setSyncError(err instanceof ApiError ? err.message : 'Сверка не прошла.')
    },
  })
  const [period, setPeriod] = useState<Period>('today')
  const [filter, setFilter] = useState<Filter>('all')
  const [cursor, setCursor] = useState<string | null>(null)
  const [pages, setPages] = useState<Trade[]>([])

  const feed = useQuery<Feed>({
    queryKey: ['trades', period, filter, cursor],
    queryFn: () => {
      const params = new URLSearchParams({ period, filter, limit: '50' })
      if (cursor) params.set('cursor', cursor)
      return api.get<Feed>(`/trades?${params.toString()}`)
    },
  })

  const metrics = useQuery<MarkingMetrics>({
    queryKey: ['marking-metrics', period],
    queryFn: () => api.get<MarkingMetrics>(`/trades/metrics?period=${period}`),
    enabled: hasSource,
  })

  const [markError, setMarkError] = useState('')
  const [markEffects, setMarkEffects] = useState<MarkedOut['effects'] | null>(null)
  const mark = useMutation({
    mutationFn: (args: { id: string; marking: string }) =>
      api.put<MarkedOut>(`/trades/${args.id}/marking`, { marking: args.marking }),
    onSuccess: (data) => {
      setMarkError('')
      setMarkEffects(data.effects)
      qc.invalidateQueries({ queryKey: ['trades'] })
      qc.invalidateQueries({ queryKey: ['marking-metrics'] })
      // Отметка может мгновенно включить блокировку: экран «Сегодня» должен
      // узнать об этом сразу, а не на следующей перезагрузке.
      qc.invalidateQueries({ queryKey: ['today'] })
      qc.invalidateQueries({ queryKey: ['curve'] })
    },
    onError: (err) => {
      setMarkEffects(null)
      setMarkError(err instanceof ApiError ? err.message : 'Не получилось отметить.')
    },
  })

  const curve = useQuery<Curve>({
    queryKey: ['curve'],
    queryFn: () => api.get<Curve>('/trades/day-curve'),
    enabled: period === 'today',
  })

  function reset(next: { period?: Period; filter?: Filter }) {
    if (next.period) setPeriod(next.period)
    if (next.filter) setFilter(next.filter)
    setCursor(null)
    setPages([])
  }

  const items = [...pages, ...(feed.data?.items ?? [])]
  const totals = feed.data?.totals

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 16,
        flexGrow: 1,
        minHeight: 0,
      }}
    >
      {!hasSource && (
        <div className="card" style={{ padding: '16px 18px' }}>
          <div style={{ color: 'var(--dim)' }}>Источник сделок не подключён.</div>
          <div className="hint" style={{ marginTop: 6 }}>
            Вставь ключ TMM на экране настроек — или подключи там тестовый источник.
          </div>
        </div>
      )}

      {hasSource && isFake && (
        <DevPanel positions={Boolean(active?.capabilities.provides_positions)} />
      )}

      {hasSource && !isFake && (
        <div className="card" style={{ padding: '14px 18px' }}>
          <div
            style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}
          >
            <button
              onClick={() => sync.mutate()}
              disabled={sync.isPending}
              style={{ fontSize: 12, padding: '6px 12px' }}
            >
              {sync.isPending ? 'Сверяю…' : 'Сверить сейчас'}
            </button>
            <span className="hint" style={{ flex: 1 }}>
              Сверка забирает сделки у источника. Она идёт и сама: раз в десять
              минут, а у источника с потоком исполнения приезжают сразу.
              Кнопка — чтобы не ждать.
            </span>
          </div>
          {syncReport && (
            <div className="hint mono" style={{ marginTop: 8 }}>
              получено {syncReport.received}, принято {syncReport.inserted},
              переразмечено {syncReport.remarked}, без изменений{' '}
              {syncReport.unchanged}, до отсчёта{' '}
              {syncReport.skipped_before_ingest_from}, открытых{' '}
              {syncReport.skipped_open}, чужой счёт{' '}
              {syncReport.skipped_unknown_account}
            </div>
          )}
          {syncError && (
            <div className="err" style={{ marginTop: 8 }}>
              {syncError}
            </div>
          )}
        </div>
      )}

      {period === 'today' && curve.data && <DayCurve curve={curve.data} />}

      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        {PERIODS.map((p) => (
          <button
            key={p.key}
            onClick={() => reset({ period: p.key })}
            className={period === p.key ? 'primary' : ''}
            style={{ fontSize: 13, padding: '7px 14px' }}
          >
            {p.label}
          </button>
        ))}
        <span style={{ width: 12 }} />
        {FILTERS.map((f) => (
          <button
            key={f.key}
            onClick={() => reset({ filter: f.key })}
            className={filter === f.key ? 'primary' : ''}
            style={{ fontSize: 13, padding: '7px 14px' }}
          >
            {f.label}
          </button>
        ))}
      </div>

      {totals && (
        <div
          className="card"
          style={{ padding: '14px 18px', display: 'flex', gap: 26, flexWrap: 'wrap' }}
        >
          <Stat label="Сделок" value={String(totals.count)} />
          <Stat label="Значимых" value={String(totals.significant_count)} />
          <Stat
            label="Результат"
            value={money(totals.profit_usd)}
            color={pnlColor(totals.profit_usd)}
          />
          <Stat
            label="От депозита"
            value={pct(totals.account_return_pct)}
            color={pnlColor(totals.account_return_pct)}
          />
          <Stat
            label="Нарушений"
            value={String(totals.violations_count)}
            color={totals.violations_count > 0 ? 'var(--bad)' : undefined}
          />
          <Stat
            label="Покрытие разметкой"
            value={`${Number(totals.coverage_pct).toFixed(0)}%`}
            color={Number(totals.coverage_pct) < 80 ? 'var(--warn)' : 'var(--ok)'}
          />
        </div>
      )}

      {metrics.data && (
        <div
          className="card"
          style={{ padding: '14px 18px', display: 'flex', gap: 26, flexWrap: 'wrap' }}
        >
          <Stat
            label="Дисциплина"
            value={
              metrics.data.marking.discipline_pct === null
                ? '—'
                : `${Number(metrics.data.marking.discipline_pct).toFixed(0)}%`
            }
          />
          <Stat
            label="Цена эмоций"
            value={money(metrics.data.marking.emotion_cost_usd)}
            color={pnlColor(metrics.data.marking.emotion_cost_usd)}
          />
          {metrics.data.marking.violations.profitable > 0 && (
            <Stat
              label="Нарушения в плюс"
              value={String(metrics.data.marking.violations.profitable)}
              color="var(--warn)"
            />
          )}
          {!metrics.data.confidence.enough_data && (
            <div className="hint" style={{ alignSelf: 'center', maxWidth: 320 }}>
              Мало данных: {metrics.data.confidence.days_available}{' '}
              {plural(
                metrics.data.confidence.days_available,
                'торговый день',
                'торговых дня',
                'торговых дней',
              )}{' '}
              из {metrics.data.confidence.days_required}. Выводы делать рано.
            </div>
          )}
        </div>
      )}

      {!provideTags && hasSource && (
        <div className="hint">
          Тегов у этого источника нет, поэтому нарушения отмечаешь ты — кнопками
          в строке сделки. Отметка «нарушение» сразу поднимает системный триггер
          SR-1, и снять её после этого нельзя: инцидент неудаляем.
        </div>
      )}

      {markError && <div className="err">{markError}</div>}
      {markEffects?.lock_started && (
        <div className="card" style={{ padding: '12px 16px', borderLeft: '3px solid var(--bad)' }}>
          Отметка включила блокировку до конца торгового дня этой сделки. Открой
          «Сегодня», чтобы снять её разбором.
        </div>
      )}
      {markEffects?.incident_opened && !markEffects.lock_started && (
        <div className="card" style={{ padding: '12px 16px', borderLeft: '3px solid var(--warn)' }}>
          Инцидент записан за {markEffects.incident_opened.day}.{' '}
          {markEffects.incident_opened.outcome === 'breached'
            ? 'После этой сделки в тот день ещё были входы — окно нарушено.'
            : 'После этой сделки в тот день входов не было — окно соблюдено.'}{' '}
          Блокировки нет: окно того дня уже закрылось.
        </div>
      )}

      <div
        className="card"
        style={{ padding: '4px 0', flexGrow: 1, minHeight: 0, overflowY: 'auto' }}
      >
        {feed.isLoading && <div className="hint" style={{ padding: 16 }}>загрузка…</div>}
        {!feed.isLoading && items.length === 0 && (
          <div className="hint" style={{ padding: 16 }}>
            {filter === 'violations'
              ? 'Нарушений за период нет. Нарушением становится сделка, которую ты '
                + 'отметил сам или у которой стоит тег, отмеченный как нарушение.'
              : 'Сделок за период нет.'}
          </div>
        )}
        {items.map((trade) => (
          <Row
            key={trade.id}
            trade={trade}
            onMark={
              provideTags
                ? undefined
                : (marking) => mark.mutate({ id: trade.id, marking })
            }
            busy={mark.isPending}
          />
        ))}
      </div>

      {feed.data?.has_more && (
        <button
          onClick={() => {
            setPages(items)
            setCursor(feed.data!.next_cursor)
          }}
          style={{ alignSelf: 'flex-start' }}
        >
          Показать ещё
        </button>
      )}
    </div>
  )
}

function Stat({
  label,
  value,
  color,
}: {
  label: string
  value: string
  color?: string
}) {
  return (
    <div>
      <div className="klabel" style={{ marginBottom: 4 }}>
        {label}
      </div>
      <div className="mono" style={{ fontSize: 15, color: color ?? 'var(--fg)' }}>
        {value}
      </div>
    </div>
  )
}

function Row({
  trade,
  onMark,
  busy,
}: {
  trade: Trade
  onMark?: (marking: string) => void
  busy?: boolean
}) {
  const mark = MARKING[trade.marking]
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 14,
        padding: '10px 18px',
        borderBottom: '1px solid #212120',
        fontSize: 13,
      }}
    >
      <span className="mono" style={{ color: 'var(--dim)', width: 44 }}>
        {time(trade.close_time)}
      </span>
      <span style={{ width: 90, fontWeight: 500 }}>{trade.symbol}</span>
      <span style={{ width: 52, color: 'var(--dim)' }}>
        {trade.side === 'long' ? 'лонг' : 'шорт'}
      </span>
      <span
        className="mono"
        style={{ width: 88, textAlign: 'right', color: pnlColor(trade.profit_usd) }}
      >
        {money(trade.profit_usd)}
      </span>
      <span
        className="mono"
        style={{
          width: 72,
          textAlign: 'right',
          color: pnlColor(trade.account_return_pct),
        }}
      >
        {pct(trade.account_return_pct)}
      </span>
      <span style={{ width: 70, color: 'var(--faint)' }}>
        {duration(trade.duration_sec)}
      </span>
      {!trade.is_significant && (
        <span
          className="hint"
          title="Мельче порога значимости: не влияет на серии убытков"
        >
          пыль
        </span>
      )}
      {trade.tags.map((tag) => (
        <span
          key={tag.external_id}
          style={{
            fontSize: 11,
            padding: '3px 9px',
            borderRadius: 12,
            border: '1px solid var(--line-2)',
            color: 'var(--dim)',
            whiteSpace: 'nowrap',
          }}
        >
          {tag.name}
        </span>
      ))}
      {/* Кнопки своей разметки прижимаются к правому краю, а готовый статус
          стоит сразу за тегами: в прототипе «Разметка» — это колонка, которая
          занимает остаток строки, а не отдельный столбец у края. */}
      {onMark && <div style={{ flexGrow: 1 }} />}
      {onMark ? (
        <div style={{ display: 'flex', gap: 6, width: 190, justifyContent: 'flex-end' }}>
          <button
            onClick={() => onMark('clean')}
            disabled={busy}
            aria-pressed={trade.marking === 'clean'}
            style={{
              fontSize: 11,
              padding: '4px 10px',
              color: trade.marking === 'clean' ? 'var(--ok)' : 'var(--faint)',
              borderColor: trade.marking === 'clean' ? '#2f4a38' : 'var(--line-2)',
            }}
          >
            по системе
          </button>
          <button
            onClick={() => onMark('violation')}
            disabled={busy}
            aria-pressed={trade.marking === 'violation'}
            style={{
              fontSize: 11,
              padding: '4px 10px',
              color: trade.marking === 'violation' ? 'var(--bad)' : 'var(--faint)',
              borderColor: trade.marking === 'violation' ? '#5a3a34' : 'var(--line-2)',
            }}
          >
            нарушение
          </button>
        </div>
      ) : (
        <span style={{ color: mark.color, width: 110 }}>{mark.label}</span>
      )}
    </div>
  )
}
