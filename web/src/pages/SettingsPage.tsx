import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { ApiError, api } from '../lib/api'
import type { Me, SessionRow, Settings, SettingsPatched } from '../lib/types'

const TIMEZONES = [
  'Europe/Moscow',
  'Europe/Kyiv',
  'Europe/Berlin',
  'Europe/London',
  'Asia/Almaty',
  'Asia/Tbilisi',
  'Asia/Dubai',
  'Asia/Bangkok',
  'America/New_York',
  'UTC',
]

type Draft = {
  timezone: string
  day_cutoff: string
  pass_score: string
  min_score: string
  significance_pct: string
  shadow_mode: boolean
}

function toDraft(s: Settings): Draft {
  return {
    timezone: s.timezone,
    day_cutoff: s.day_cutoff.slice(0, 5),
    pass_score: String(s.pass_score),
    min_score: String(s.min_score),
    significance_pct: String(s.significance_pct),
    shadow_mode: s.shadow_mode,
  }
}

export function SettingsPage({ me }: { me: Me }) {
  const qc = useQueryClient()
  const [draft, setDraft] = useState<Draft>(toDraft(me.settings))
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [saved, setSaved] = useState(false)

  useEffect(() => setDraft(toDraft(me.settings)), [me.settings])

  const save = useMutation({
    mutationFn: async () =>
      api.patch<SettingsPatched>('/me/settings', {
        timezone: draft.timezone,
        day_cutoff: `${draft.day_cutoff}:00`,
        pass_score: Number(draft.pass_score),
        min_score: Number(draft.min_score),
        significance_pct: draft.significance_pct,
        shadow_mode: draft.shadow_mode,
      }),
    onSuccess: (data) => {
      setError('')
      setNotice(data.notice ?? '')
      setSaved(true)
      qc.setQueryData<Me | null>(['me'], (old) =>
        old ? { ...old, settings: data.settings } : old,
      )
    },
    onError: (err) => {
      setSaved(false)
      setNotice('')
      setError(err instanceof ApiError ? err.message : 'Не удалось сохранить.')
    },
  })

  const sessions = useQuery<SessionRow[]>({
    queryKey: ['sessions'],
    queryFn: () => api.get<SessionRow[]>('/me/sessions'),
  })

  const revoke = useMutation({
    mutationFn: (id: string) => api.del(`/me/sessions/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['sessions'] }),
  })

  function set<K extends keyof Draft>(key: K, value: Draft[K]) {
    setDraft((d) => ({ ...d, [key]: value }))
    setSaved(false)
  }

  const row = { display: 'flex', alignItems: 'center', gap: 12, marginBottom: 14 } as const
  const label = { width: 210, fontSize: 13, color: 'var(--dim)' } as const

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16, maxWidth: 680 }}>
      <div className="card" style={{ padding: '18px 20px' }}>
        <div className="klabel" style={{ marginBottom: 16 }}>
          Торговый день
        </div>

        <div style={row}>
          <label style={label} htmlFor="tz">
            Таймзона
          </label>
          <select
            id="tz"
            value={draft.timezone}
            onChange={(e) => set('timezone', e.target.value)}
            style={{ flexGrow: 1 }}
          >
            {(TIMEZONES.includes(draft.timezone)
              ? TIMEZONES
              : [draft.timezone, ...TIMEZONES]
            ).map((tz) => (
              <option key={tz} value={tz}>
                {tz}
              </option>
            ))}
          </select>
        </div>

        <div style={row}>
          <label style={label} htmlFor="cutoff">
            Граница дня
          </label>
          <input
            id="cutoff"
            type="time"
            value={draft.day_cutoff}
            onChange={(e) => set('day_cutoff', e.target.value)}
          />
        </div>

        <div className="hint">
          Сделка относится к торговому дню по времени открытия. При смене таймзоны или
          границы прошлые дни не пересчитываются: они остаются такими, какими были
          записаны.
        </div>
      </div>

      <div className="card" style={{ padding: '18px 20px' }}>
        <div className="klabel" style={{ marginBottom: 16 }}>
          Пороги
        </div>

        <div style={row}>
          <label style={label} htmlFor="pass">
            Проходной балл чека
          </label>
          <input
            id="pass"
            className="mono"
            type="number"
            min={1}
            max={25}
            value={draft.pass_score}
            onChange={(e) => set('pass_score', e.target.value)}
            style={{ width: 90 }}
          />
          <span className="hint">из 25</span>
        </div>

        <div style={row}>
          <label style={label} htmlFor="min">
            Минимальный балл
          </label>
          <input
            id="min"
            className="mono"
            type="number"
            min={1}
            max={25}
            value={draft.min_score}
            onChange={(e) => set('min_score', e.target.value)}
            style={{ width: 90 }}
          />
          <span className="hint">ниже — допуска нет</span>
        </div>

        <div style={row}>
          <label style={label} htmlFor="dust">
            Порог значимости сделки
          </label>
          <input
            id="dust"
            className="mono"
            type="text"
            inputMode="decimal"
            value={draft.significance_pct}
            onChange={(e) => set('significance_pct', e.target.value)}
            style={{ width: 90 }}
          />
          <span className="hint">% депозита</span>
        </div>

        <div className="hint">
          Сделки мельче порога не прерывают и не удлиняют серии убытков: иначе мелкая
          прибыльная сделка между двумя стопами спасала бы от блокировки.
        </div>
      </div>

      <div className="card" style={{ padding: '18px 20px' }}>
        <div className="klabel" style={{ marginBottom: 14 }}>
          Режим наблюдения
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <button
            type="button"
            aria-pressed={draft.shadow_mode}
            onClick={() => set('shadow_mode', !draft.shadow_mode)}
            className={draft.shadow_mode ? 'primary' : ''}
            style={{ fontSize: 12, padding: '6px 14px' }}
          >
            {draft.shadow_mode ? 'включён' : 'выключен'}
          </button>
          <span className="hint" style={{ flex: 1 }}>
            Правила считаются и инциденты пишутся, но блокировки не применяются и
            уведомления не отправляются. Стрик при этом считается по-настоящему.
          </span>
        </div>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
        <button
          className="primary"
          onClick={() => save.mutate()}
          disabled={save.isPending}
        >
          {save.isPending ? 'Сохраняю…' : 'Сохранить'}
        </button>
        {saved && !error && <span className="ok-text">Сохранено</span>}
        {error && (
          <span className="err" role="alert">
            {error}
          </span>
        )}
      </div>
      {notice && <div className="hint">{notice}</div>}

      <div className="card" style={{ padding: '18px 20px' }}>
        <div className="klabel" style={{ marginBottom: 14 }}>
          Активные входы
        </div>
        {sessions.isLoading && <div className="hint">загрузка…</div>}
        {sessions.data?.map((s) => (
          <div
            key={s.id}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 12,
              padding: '8px 0',
              borderBottom: '1px solid #212120',
              fontSize: 13,
            }}
          >
            <span className="mono" style={{ color: 'var(--dim)' }}>
              {new Date(s.last_seen_at).toLocaleString('ru-RU')}
            </span>
            <span className="hint" style={{ flexGrow: 1, minWidth: 0 }}>
              {s.user_agent?.slice(0, 60) ?? 'неизвестное устройство'}
            </span>
            {s.current ? (
              <span className="hint">этот</span>
            ) : (
              <button
                onClick={() => revoke.mutate(s.id)}
                style={{ fontSize: 12, padding: '5px 11px' }}
              >
                Отключить
              </button>
            )}
          </div>
        ))}
        <div className="hint" style={{ marginTop: 10 }}>
          Сессия живёт 30 дней. Отключение здесь обрывает вход на том устройстве сразу.
        </div>
      </div>
    </div>
  )
}
