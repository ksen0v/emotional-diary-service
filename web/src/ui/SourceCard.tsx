import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { ApiError, api } from '../lib/api'
import type {
  Connection,
  ConnectResult,
  Connections,
  Probe,
  SwitchConsequences,
} from '../lib/types'
import { ProbeView, SourceConnect } from './SourceConnect'
import { dateTime } from './format'

const LABELS: Record<string, string> = {
  fake: 'Тестовый источник',
  tmm: 'TMM (tradermake.money)',
  binance: 'Binance Futures',
}

const STATES: Record<string, { label: string; color: string }> = {
  connected: { label: 'подключён', color: 'var(--ok)' },
  error: { label: 'ошибка', color: 'var(--bad)' },
  paused: { label: 'на паузе', color: 'var(--warn)' },
}

export function useConnections() {
  return useQuery<Connections>({
    queryKey: ['connections'],
    queryFn: () => api.get<Connections>('/source/connections'),
  })
}

export function SourceCard() {
  const qc = useQueryClient()
  const connections = useConnections()
  const rows = connections.data?.connections ?? []
  // Настоящий источник может быть только один активный, но подключены могут
  // быть оба: переключение между ними — отдельная операция с подтверждением.
  const real = rows.filter((c) => c.provider !== 'fake')

  const [replaceKey, setReplaceKey] = useState<string | null>(null)
  const [switching, setSwitching] = useState<
    { id: string; details: SwitchConsequences } | null
  >(null)
  const [removing, setRemoving] = useState<string | null>(null)
  const [probe, setProbe] = useState<Probe | null>(null)
  const [error, setError] = useState('')

  function refresh() {
    qc.invalidateQueries({ queryKey: ['connections'] })
    qc.invalidateQueries({ queryKey: ['tags'] })
    qc.invalidateQueries({ queryKey: ['trades'] })
    qc.invalidateQueries({ queryKey: ['marking-metrics'] })
    qc.invalidateQueries({ queryKey: ['curve'] })
    qc.invalidateQueries({ queryKey: ['me'] })
  }

  const activate = useMutation({
    mutationFn: (args: { id: string; confirm: boolean }) =>
      api.post<Connection>(`/source/connections/${args.id}/activate`, {
        confirm: args.confirm,
      }),
    onSuccess: () => {
      setSwitching(null)
      setError('')
      refresh()
    },
    onError: (err, args) => {
      if (err instanceof ApiError && err.code === 'confirmation_required') {
        // Сервер прислал список последствий — показываем его, а не сочиняем свой.
        setSwitching({ id: args.id, details: err.details as SwitchConsequences })
        return
      }
      setError(message(err))
    },
  })

  const verify = useMutation({
    mutationFn: (id: string) =>
      api.post<ConnectResult>(`/source/connections/${id}/verify`),
    onSuccess: (data) => {
      setProbe(data.probe)
      setError('')
      refresh()
    },
    onError: (err) => {
      setProbe(null)
      setError(message(err))
    },
  })

  const remove = useMutation({
    mutationFn: (id: string) => api.del<void>(`/source/connections/${id}`),
    onSuccess: () => {
      setRemoving(null)
      setProbe(null)
      refresh()
    },
    onError: (err) => setError(message(err)),
  })

  return (
    <div className="card" style={{ padding: '18px 20px' }}>
      <div className="klabel" style={{ marginBottom: 14 }}>
        Источник сделок
      </div>

      {rows.length === 0 && (
        <div className="hint" style={{ marginBottom: 14 }}>
          Ничего не подключено. Вставь ключ, чтобы сервис начал видеть сделки.
        </div>
      )}

      {rows.map((row) => (
        <div
          key={row.id}
          style={{
            paddingBottom: 14,
            marginBottom: 14,
            borderBottom: '1px solid #2b2b27',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
            <span>{LABELS[row.provider] ?? row.provider}</span>
            {row.is_active && (
              <span
                style={{
                  fontSize: 11,
                  padding: '3px 9px',
                  borderRadius: 12,
                  border: '1px solid #2f4a38',
                  color: 'var(--ok)',
                }}
              >
                активен
              </span>
            )}
            <span
              style={{ fontSize: 11, color: (STATES[row.state] ?? STATES.paused).color }}
            >
              {(STATES[row.state] ?? { label: row.state }).label}
            </span>
            {row.key_masked && row.provider !== 'fake' && (
              <span className="mono hint">{row.key_masked}</span>
            )}
            <div style={{ flexGrow: 1 }} />
            <span className="hint">
              счёт: {row.accounts.map((a) => a.name).join(', ') || '—'}
            </span>
          </div>

          <div className="hint" style={{ marginTop: 8 }}>
            Отсчёт с {dateTime(row.ingest_from)}: сделки, закрытые раньше, сервис
            не видит.
          </div>

          {row.last_error && (
            <div className="err" style={{ marginTop: 8 }}>
              {row.last_error}
            </div>
          )}

          <div style={{ display: 'flex', gap: 8, marginTop: 12, flexWrap: 'wrap' }}>
            {!row.is_active && (
              <button
                onClick={() => activate.mutate({ id: row.id, confirm: false })}
                disabled={activate.isPending}
                style={{ fontSize: 12, padding: '6px 12px' }}
              >
                Сделать активным
              </button>
            )}
            {row.provider !== 'fake' && (
              <button
                onClick={() => verify.mutate(row.id)}
                disabled={verify.isPending}
                style={{ fontSize: 12, padding: '6px 12px' }}
              >
                {verify.isPending ? 'Проверяю…' : 'Проверить'}
              </button>
            )}
            {row.provider !== 'fake' && (
              <button
                onClick={() => setReplaceKey(replaceKey === row.provider ? null : row.provider)}
                style={{ fontSize: 12, padding: '6px 12px' }}
              >
                {replaceKey === row.provider ? 'Не менять ключ' : 'Заменить ключ'}
              </button>
            )}
            {removing === row.id ? (
              <>
                <button
                  className="primary"
                  onClick={() => remove.mutate(row.id)}
                  disabled={remove.isPending}
                  style={{ fontSize: 12, padding: '6px 12px' }}
                >
                  Удалить насовсем
                </button>
                <button
                  onClick={() => setRemoving(null)}
                  style={{ fontSize: 12, padding: '6px 12px' }}
                >
                  Отмена
                </button>
                <span className="hint" style={{ alignSelf: 'center' }}>
                  Ключ удалится, принятые сделки останутся.
                </span>
              </>
            ) : (
              <button
                onClick={() => setRemoving(row.id)}
                style={{ fontSize: 12, padding: '6px 12px' }}
              >
                Удалить
              </button>
            )}
          </div>

        </div>
      ))}

      {switching && (
        <div
          style={{
            border: '1px solid #5a3a34',
            borderRadius: 8,
            padding: '12px 14px',
            marginBottom: 14,
          }}
        >
          <div style={{ marginBottom: 8 }}>Переключение источника меняет расчёты.</div>
          <ul className="hint" style={{ margin: '0 0 10px 18px' }}>
            {(switching.details.consequences ?? []).map((line) => (
              <li key={line} style={{ marginBottom: 4 }}>
                {line}
              </li>
            ))}
          </ul>
          <div style={{ display: 'flex', gap: 8 }}>
            <button
              className="primary"
              onClick={() => activate.mutate({ id: switching.id, confirm: true })}
              disabled={activate.isPending}
              style={{ fontSize: 12, padding: '6px 12px' }}
            >
              Переключить
            </button>
            <button
              onClick={() => setSwitching(null)}
              style={{ fontSize: 12, padding: '6px 12px' }}
            >
              Отмена
            </button>
          </div>
        </div>
      )}

      {error && (
        <div className="err" style={{ marginBottom: 12 }}>
          {error}
        </div>
      )}

      {probe && (
        <div style={{ marginBottom: 14 }}>
          <ProbeView probe={probe} />
        </div>
      )}

      {(real.length === 0 || replaceKey !== null) && (
        <div style={{ marginBottom: 14 }}>
          <SourceConnect
            initial={replaceKey === 'binance' ? 'binance' : 'tmm'}
            replacing={replaceKey !== null}
            onDone={() => setReplaceKey(null)}
          />
        </div>
      )}
      {real.length > 0 && replaceKey === null && real.length < 2 && (
        <div style={{ marginBottom: 14 }}>
          <button
            onClick={() => setReplaceKey(real[0].provider === 'tmm' ? 'binance' : 'tmm')}
            style={{ fontSize: 12, padding: '6px 12px' }}
          >
            Подключить второй источник
          </button>
          <div className="hint" style={{ marginTop: 6 }}>
            Активным остаётся один: переключение — отдельная кнопка, с
            подтверждением. Метрики и стрик считаются только по активному.
          </div>
        </div>
      )}

    </div>
  )
}

function message(err: unknown): string {
  return err instanceof ApiError ? err.message : 'Не получилось.'
}
