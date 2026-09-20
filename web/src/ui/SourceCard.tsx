import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, api } from '../lib/api'
import type { Connection, Connections } from '../lib/types'
import { dateTime } from './format'

const LABELS: Record<string, string> = {
  fake: 'Тестовый источник',
  tmm: 'TMM (tradermake.money)',
  binance: 'Binance Futures',
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

  const connect = useMutation({
    mutationFn: () => api.post<Connection>('/source/connections/fake'),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['connections'] })
      qc.invalidateQueries({ queryKey: ['me'] })
    },
  })

  const active = connections.data?.connections.find((c) => c.is_active)

  return (
    <div className="card" style={{ padding: '18px 20px' }}>
      <div className="klabel" style={{ marginBottom: 14 }}>
        Источник сделок
      </div>

      {active ? (
        <>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <span>{LABELS[active.provider] ?? active.provider}</span>
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
            <div style={{ flexGrow: 1 }} />
            <span className="hint">счёт: {active.accounts[0]?.name ?? '—'}</span>
          </div>
          <div className="hint" style={{ marginTop: 10 }}>
            Отсчёт с {dateTime(active.ingest_from)}: сделки, закрытые раньше, сервис
            не видит. Истории не импортируем — метрики начинаются с этого момента.
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 10 }}>
            {Object.entries(active.capabilities).map(([key, value]) => (
              <span
                key={key}
                className="mono"
                style={{
                  fontSize: 11,
                  padding: '3px 9px',
                  borderRadius: 12,
                  border: '1px solid var(--line-2)',
                  color: value === true ? 'var(--ok)' : 'var(--faint)',
                }}
              >
                {key}
                {typeof value === 'string' ? `: ${value}` : ''}
              </span>
            ))}
          </div>
        </>
      ) : (
        <>
          <div style={{ color: 'var(--dim)' }}>Ничего не подключено.</div>
          <div className="hint" style={{ marginTop: 6, marginBottom: 12 }}>
            Подключение TMM появится на шаге 4, Binance — на шаге 14. Пока доступен
            тестовый источник: он ведёт себя как настоящий, но сделки в него подаёшь ты.
          </div>
          <button
            className="primary"
            onClick={() => connect.mutate()}
            disabled={connect.isPending}
          >
            {connect.isPending ? 'Подключаю…' : 'Подключить тестовый источник'}
          </button>
          {connect.isError && (
            <div className="err" style={{ marginTop: 10 }}>
              {connect.error instanceof ApiError
                ? connect.error.message
                : 'Не получилось подключить.'}
            </div>
          )}
        </>
      )}
    </div>
  )
}
