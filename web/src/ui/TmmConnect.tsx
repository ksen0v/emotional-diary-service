import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { ApiError, api } from '../lib/api'
import type { ConnectResult, Probe } from '../lib/types'
import { duration, money, pct, pnlColor, time } from './format'

// Текст про read-only взят из ТЗ 4.1 дословно. Он честный по сути: режим ключа
// через API не проверить, поэтому обещать «мы убедились» нельзя.
const READ_ONLY = 'Создай ключ в режиме read-only — сервис только читает дневник. '
  + 'Проверить режим ключа через API мы не можем, поэтому это на тебе. '
  + 'Храни ключ как пароль.'

export function TmmConnect({
  replacing = false,
  onDone,
}: {
  replacing?: boolean
  onDone?: () => void
}) {
  const qc = useQueryClient()
  const [key, setKey] = useState('')
  const [result, setResult] = useState<ConnectResult | null>(null)
  const [error, setError] = useState('')

  const connect = useMutation({
    mutationFn: () =>
      api.post<ConnectResult>('/source/connections', {
        provider: 'tmm',
        key: key.trim(),
      }),
    onSuccess: (data) => {
      setResult(data)
      setError('')
      setKey('')
      qc.invalidateQueries({ queryKey: ['connections'] })
      qc.invalidateQueries({ queryKey: ['tags'] })
      qc.invalidateQueries({ queryKey: ['me'] })
      qc.invalidateQueries({ queryKey: ['trades'] })
      onDone?.()
    },
    onError: (err) => {
      setResult(null)
      setError(err instanceof ApiError ? err.message : 'Не получилось подключить.')
    },
  })

  return (
    <div>
      <div className="klabel" style={{ marginBottom: 8 }}>
        {replacing ? 'Заменить ключ TMM' : 'Подключить TMM'}
      </div>
      <div className="hint" style={{ marginBottom: 10, maxWidth: 620 }}>
        {READ_ONLY}
      </div>
      <div className="hint" style={{ marginBottom: 12, maxWidth: 620 }}>
        Ключ создаётся в личном кабинете tradermake.money. Сервис начнёт считать
        с момента подключения: сделки, закрытые раньше, в него не попадут.
      </div>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        <input
          type="password"
          value={key}
          autoComplete="off"
          onChange={(e) => setKey(e.target.value)}
          placeholder="Ключ API TMM"
          className="mono"
          style={{ minWidth: 320, flex: 1 }}
        />
        <button
          className="primary"
          onClick={() => connect.mutate()}
          disabled={connect.isPending || key.trim().length < 8}
        >
          {connect.isPending ? 'Проверяю ключ…' : replacing ? 'Заменить' : 'Подключить'}
        </button>
      </div>
      {error && (
        <div className="err" style={{ marginTop: 10 }}>
          {error}
        </div>
      )}
      {result && (
        <div style={{ marginTop: 14 }}>
          {result.warnings.map((w) => (
            <div
              key={w.code}
              style={{ color: 'var(--warn)', fontSize: 13, marginBottom: 8 }}
            >
              {w.message}
            </div>
          ))}
          <ProbeView probe={result.probe} />
        </div>
      )}
    </div>
  )
}

export function ProbeView({ probe }: { probe: Probe }) {
  return (
    <div
      style={{
        border: '1px solid var(--line-2)',
        borderRadius: 8,
        padding: '12px 14px',
      }}
    >
      <div className="klabel" style={{ marginBottom: 8 }}>
        Что видно по ключу
      </div>
      <div className="hint" style={{ marginBottom: 10 }}>
        Проба за последние 30 дней. Эти сделки показаны как есть и в сервис
        не попадают: историю мы не импортируем, считать начинаем с подключения.
      </div>
      <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap', marginBottom: 10 }}>
        <Small label="Счетов" value={String(probe.accounts.length)} />
        <Small label="Тегов входа" value={String(probe.entry_tags.length)} />
        <Small label="Сделок за 30 дней" value={String(probe.trades_seen)} />
      </div>
      {probe.accounts.length > 0 && (
        <div className="hint" style={{ marginBottom: 10 }}>
          Счета: {probe.accounts.map((a) => a.name).join(', ')}
        </div>
      )}
      {probe.sample.length === 0 ? (
        <div className="hint">
          Закрытых сделок за 30 дней провайдер не отдал. Ключ рабочий — иначе
          подключение бы не прошло.
        </div>
      ) : (
        <div>
          {probe.sample.map((row) => (
            <div
              key={row.external_id}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 12,
                padding: '6px 0',
                borderTop: '1px solid #212120',
                fontSize: 13,
              }}
            >
              <span className="mono" style={{ color: 'var(--dim)', width: 44 }}>
                {row.close_time ? time(row.close_time) : '—'}
              </span>
              <span style={{ width: 92, fontWeight: 500 }}>{row.symbol}</span>
              <span style={{ width: 48, color: 'var(--dim)' }}>
                {row.side === 'long' ? 'лонг' : 'шорт'}
              </span>
              <span
                className="mono"
                style={{ width: 86, textAlign: 'right', color: pnlColor(row.profit_usd) }}
              >
                {money(row.profit_usd)}
              </span>
              <span
                className="mono"
                style={{
                  width: 70,
                  textAlign: 'right',
                  color: pnlColor(row.account_return_pct),
                }}
              >
                {pct(row.account_return_pct)}
              </span>
              <span style={{ width: 66, color: 'var(--faint)' }}>
                {duration(row.duration_sec)}
              </span>
              <span className="hint" style={{ flex: 1 }}>
                {row.tags.length ? row.tags.join(', ') : 'без тегов входа'}
              </span>
            </div>
          ))}
        </div>
      )}
      {probe.window_filter_honored === false && (
        <div className="hint" style={{ marginTop: 10, color: 'var(--warn)' }}>
          Провайдер отдал сделки шире запрошенного окна — лишние отсечены на нашей
          стороне. На данные это не влияет, но запрос стоит уточнить.
        </div>
      )}
      {probe.mapping_errors.length > 0 && (
        <div className="hint" style={{ marginTop: 10, color: 'var(--bad)' }}>
          Не разобрано: {probe.mapping_errors.join('; ')}
        </div>
      )}
    </div>
  )
}

function Small({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="klabel" style={{ marginBottom: 3 }}>
        {label}
      </div>
      <div className="mono" style={{ fontSize: 15 }}>
        {value}
      </div>
    </div>
  )
}
