import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { ApiError, api } from '../lib/api'
import type { SyncReport } from '../lib/types'

// Панель разработки: кладёт сделку в фейковый источник и запускает сверку.
// Она сознательно не пишет в таблицу сделок напрямую — иначе проверялся бы
// не тот путь, по которому пойдут настоящие данные.
const PRESETS = [
  { label: 'Стоп по системе', profit: '-84.20', pct: '-0.68', tags: ['ПО СИСТЕМЕ'] },
  { label: 'Стоп без разметки', profit: '-91.00', pct: '-0.72', tags: [] },
  {
    label: 'Несистемная сделка',
    profit: '-140.00',
    pct: '-1.10',
    tags: ['НЕ СИСТЕМНАЯ ТОРГОВЛЯ'],
  },
  { label: 'Прибыль по системе', profit: '+126.40', pct: '+1.02', tags: ['ПО СИСТЕМЕ'] },
  { label: 'Пыль', profit: '-3.10', pct: '-0.04', tags: ['ПО СИСТЕМЕ'] },
]

const SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']

export function DevPanel() {
  const qc = useQueryClient()
  const [report, setReport] = useState<SyncReport | null>(null)
  const [error, setError] = useState('')
  const [symbol, setSymbol] = useState(SYMBOLS[0])

  const push = useMutation({
    mutationFn: async (preset: (typeof PRESETS)[number]) => {
      await api.post('/source/dev/trade', {
        symbol,
        side: Number(preset.pct) < 0 ? 'long' : 'long',
        profit_usd: preset.profit,
        account_return_pct: preset.pct,
        tags: preset.tags,
        minutes_ago: 0,
        duration_sec: 420,
      })
      return api.post<SyncReport>('/sync')
    },
    onSuccess: (data) => {
      setError('')
      setReport(data)
      qc.invalidateQueries({ queryKey: ['trades'] })
      qc.invalidateQueries({ queryKey: ['curve'] })
      qc.invalidateQueries({ queryKey: ['tags'] })
    },
    onError: (err) => {
      setReport(null)
      setError(err instanceof ApiError ? err.message : 'Не получилось.')
    },
  })

  const sync = useMutation({
    mutationFn: () => api.post<SyncReport>('/sync'),
    onSuccess: (data) => {
      setError('')
      setReport(data)
      qc.invalidateQueries({ queryKey: ['trades'] })
      qc.invalidateQueries({ queryKey: ['curve'] })
    },
    onError: (err) =>
      setError(err instanceof ApiError ? err.message : 'Сверка не прошла.'),
  })

  return (
    <div className="card" style={{ padding: '16px 18px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
        <span className="klabel">Тестовый источник</span>
        <div style={{ flexGrow: 1 }} />
        <select value={symbol} onChange={(e) => setSymbol(e.target.value)}>
          {SYMBOLS.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <button onClick={() => sync.mutate()} disabled={sync.isPending}>
          Сверить
        </button>
      </div>

      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        {PRESETS.map((preset) => (
          <button
            key={preset.label}
            onClick={() => push.mutate(preset)}
            disabled={push.isPending}
            style={{ fontSize: 12, padding: '7px 12px' }}
          >
            {preset.label}
          </button>
        ))}
      </div>

      {report && (
        <div className="hint" style={{ marginTop: 12 }}>
          Сверка: получено {report.received}, принято {report.inserted}, переразмечено{' '}
          {report.remarked}, без изменений {report.unchanged}
          {report.skipped_before_ingest_from > 0 &&
            `, пропущено как «раньше подключения» ${report.skipped_before_ingest_from}`}
        </div>
      )}
      {error && (
        <div className="err" style={{ marginTop: 12 }} role="alert">
          {error}
        </div>
      )}
      <div className="hint" style={{ marginTop: 10 }}>
        Кнопка кладёт сделку в источник, а сверка забирает её оттуда — тот же путь,
        которым пойдут настоящие сделки из TMM на шаге 4.
      </div>
    </div>
  )
}
