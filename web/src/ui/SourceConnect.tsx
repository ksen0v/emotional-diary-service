import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { ApiError, api } from '../lib/api'
import type { ConnectResult, KeyPermissions, Probe } from '../lib/types'
import { duration, money, pct, pnlColor, time } from './format'

// Одна форма на оба источника, а не две карточки рядом. Причина не в экономии
// места: подключён всегда ровно один источник (ТЗ 4.2), и форма, в которой
// сначала выбираешь источник, а потом заполняешь его поля, говорит это сама.
// Две формы рядом читались бы как «подключи оба».

type Provider = 'tmm' | 'binance'

const PROVIDERS: { key: Provider; label: string; note: string }[] = [
  {
    key: 'tmm',
    label: 'TMM',
    note: 'Готовые сделки и твои теги разметки. Открытых позиций не отдаёт.',
  },
  {
    key: 'binance',
    label: 'Binance Futures',
    note: 'Настоящий баланс и открытые позиции. Тегов нет — нарушения '
      + 'отмечаются в ленте сделок.',
  },
]

// Текст про read-only у TMM взят из ТЗ 4.1 дословно. Он честный по сути:
// режим ключа через API не проверить, поэтому обещать «мы убедились» нельзя.
const TMM_READ_ONLY =
  'Создай ключ в режиме read-only — сервис только читает дневник. '
  + 'Проверить режим ключа через API мы не можем, поэтому это на тебе. '
  + 'Храни ключ как пароль.'

// У Binance всё наоборот: права ключа читаются программно, и это главный
// выигрыш этого источника по безопасности (Архитектура ч.1 §4.8).
const BINANCE_READ_ONLY =
  'Создай ключ только с правом чтения. Право вывода средств сервис проверяет '
  + 'сам и такой ключ не подключает. Привязку к IP надо выключить — наш адрес '
  + 'меняется. Секрет показывается на бирже один раз: скопируй его сразу.'

export function SourceConnect({
  initial = 'tmm',
  replacing = false,
  onDone,
}: {
  initial?: Provider
  replacing?: boolean
  onDone?: () => void
}) {
  const qc = useQueryClient()
  const [provider, setProvider] = useState<Provider>(initial)
  const [key, setKey] = useState('')
  const [secret, setSecret] = useState('')
  const [result, setResult] = useState<ConnectResult | null>(null)
  const [error, setError] = useState('')

  const binance = provider === 'binance'
  const ready = key.trim().length >= 8 && (!binance || secret.trim().length >= 8)

  const connect = useMutation({
    mutationFn: () =>
      api.post<ConnectResult>('/source/connections', {
        provider,
        market: binance ? 'futures' : null,
        key: key.trim(),
        secret: binance ? secret.trim() : null,
      }),
    onSuccess: (data) => {
      setResult(data)
      setError('')
      setKey('')
      setSecret('')
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

  function pick(next: Provider) {
    setProvider(next)
    setKey('')
    setSecret('')
    setError('')
    setResult(null)
  }

  return (
    <div>
      <div className="klabel" style={{ marginBottom: 8 }}>
        {replacing ? 'Заменить ключ' : 'Подключить источник'}
      </div>

      <div style={{ display: 'flex', gap: 8, marginBottom: 10, flexWrap: 'wrap' }}>
        {PROVIDERS.map((row) => (
          <button
            key={row.key}
            onClick={() => pick(row.key)}
            aria-pressed={provider === row.key}
            style={{
              fontSize: 13,
              padding: '7px 14px',
              border: `1px solid ${provider === row.key ? '#3c3c36' : 'var(--line-2)'}`,
              background: provider === row.key ? '#2b2b27' : 'transparent',
              color: provider === row.key ? 'var(--fg)' : 'var(--dim)',
            }}
          >
            {row.label}
          </button>
        ))}
      </div>

      <div className="hint" style={{ marginBottom: 10, maxWidth: 620 }}>
        {PROVIDERS.find((row) => row.key === provider)?.note}
      </div>
      <div className="hint" style={{ marginBottom: 10, maxWidth: 620 }}>
        {binance ? BINANCE_READ_ONLY : TMM_READ_ONLY}
      </div>
      <div className="hint" style={{ marginBottom: 12, maxWidth: 620 }}>
        Сервис начнёт считать с момента подключения: сделки, закрытые раньше,
        в него не попадут.
        {binance && ' Поддерживается только USDⓈ-M Futures, спот — нет.'}
      </div>

      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        <input
          type="password"
          value={key}
          autoComplete="off"
          onChange={(e) => setKey(e.target.value)}
          placeholder={binance ? 'API Key' : 'Ключ API TMM'}
          className="mono"
          style={{ minWidth: 280, flex: 1 }}
        />
        {binance && (
          <input
            type="password"
            value={secret}
            autoComplete="off"
            onChange={(e) => setSecret(e.target.value)}
            placeholder="Secret Key"
            className="mono"
            style={{ minWidth: 280, flex: 1 }}
          />
        )}
        <button
          className="primary"
          onClick={() => connect.mutate()}
          disabled={connect.isPending || !ready}
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

// Права ключа показываются так, как их отдала биржа. Это не украшение:
// сервис отказывает по правам, и трейдер должен видеть, на основании чего.
const RIGHTS: { key: keyof KeyPermissions; label: string; good: boolean }[] = [
  { key: 'enableReading', label: 'чтение', good: true },
  { key: 'enableWithdrawals', label: 'вывод средств', good: false },
  { key: 'enableFutures', label: 'торговля фьючерсами', good: false },
  { key: 'enableSpotAndMarginTrading', label: 'торговля на споте', good: false },
  { key: 'ipRestrict', label: 'привязка к IP', good: false },
]

export function KeyRights({ permissions }: { permissions: KeyPermissions }) {
  return (
    <div>
      <div className="klabel" style={{ marginBottom: 8 }}>
        Права ключа
      </div>
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
        {RIGHTS.map((row) => {
          const on = permissions[row.key] === true
          // Зелёный — «так и должно быть», жёлтый — «есть, но сервису не нужно».
          const color = on === row.good ? 'var(--ok)' : 'var(--warn)'
          return (
            <span
              key={row.key}
              style={{
                fontSize: 11,
                padding: '3px 9px',
                borderRadius: 12,
                border: '1px solid var(--line-2)',
                color: on ? color : 'var(--faint)',
              }}
            >
              {row.label}: {on ? 'есть' : 'нет'}
            </span>
          )
        })}
      </div>
      <div className="hint" style={{ marginTop: 8 }}>
        Снимок сделан при подключении. Права могли измениться с тех пор — кнопка
        «Проверить» перечитывает их заново.
      </div>
    </div>
  )
}

export function ProbeView({ probe }: { probe: Probe }) {
  const binance = probe.permissions !== null
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
        {binance
          ? 'Проба за последнюю неделю. Сделки собраны из исполнений прямо '
            + 'сейчас — так видно не только что ключ рабочий, но и что сборка '
            + 'сделок даёт то, что ты помнишь.'
          : 'Проба за последние 30 дней. Эти сделки показаны как есть и в сервис '
            + 'не попадают: историю мы не импортируем, считать начинаем '
            + 'с подключения.'}
      </div>
      <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap', marginBottom: 10 }}>
        <Small label="Счетов" value={String(probe.accounts.length)} />
        {binance ? (
          <Small label="Символов" value={String(probe.symbols?.length ?? 0)} />
        ) : (
          <Small label="Тегов входа" value={String(probe.entry_tags.length)} />
        )}
        <Small
          label={binance ? 'Сделок за неделю' : 'Сделок за 30 дней'}
          value={String(probe.trades_seen)}
        />
      </div>
      {probe.accounts.length > 0 && (
        <div className="hint" style={{ marginBottom: 10 }}>
          Счета: {probe.accounts.map((a) => a.name).join(', ')}
        </div>
      )}
      {probe.sample.length === 0 ? (
        <div className="hint">
          Закрытых сделок за этот период провайдер не отдал. Ключ рабочий — иначе
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
                {binance
                  ? 'разметка ставится в ленте'
                  : row.tags.length
                    ? row.tags.join(', ')
                    : 'без тегов входа'}
              </span>
            </div>
          ))}
        </div>
      )}
      {probe.permissions && (
        <div style={{ marginTop: 12 }}>
          <KeyRights permissions={probe.permissions} />
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
