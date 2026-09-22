import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { ApiError, api } from '../lib/api'
import type { DiaryEntry } from '../lib/types'
import { dateTime } from './format'
import { useEntryDraft, usePresets } from './entry'

// «Запись за сегодня» с главного экрана: оценка дня, статус, ментальные теги,
// состояние. Ровно тот состав, что в прототипе Main, и те же поля, что в ТЗ 5.1.
export function EntryEditor({
  level,
  periodStart,
  entry,
}: {
  level: 'day' | 'week' | 'month'
  periodStart: string
  entry: DiaryEntry | null
}) {
  const presets = usePresets()
  const draft = useEntryDraft({ level, periodStart, entry })

  return (
    <div
      className="card"
      style={{ padding: '14px 18px', display: 'flex', flexDirection: 'column' }}
    >
      <div style={{ display: 'flex', alignItems: 'baseline', marginBottom: 11 }}>
        <span className="klabel">Запись за сегодня</span>
        <span className="mono hint" style={{ marginLeft: 'auto' }}>
          {draft.saveHint}
        </span>
      </div>

      {draft.editable ? (
        <>
          <div style={{ fontSize: 12, color: 'var(--dim)' }}>Оценка дня</div>
          <div style={{ display: 'flex', gap: 6, margin: '7px 0 14px' }}>
            {[1, 2, 3, 4, 5].map((value) => (
              <button
                key={value}
                onClick={() => draft.setScore(draft.score === value ? null : value)}
                aria-pressed={draft.score === value}
                className={draft.score === value ? 'primary mono' : 'mono'}
                style={{ width: 34, height: 34, padding: 0, fontSize: 13 }}
              >
                {value}
              </button>
            ))}
          </div>

          <label style={{ fontSize: 12, color: 'var(--dim)' }} htmlFor="entry-status">
            Статус дня
          </label>
          <select
            id="entry-status"
            value={draft.status}
            onChange={(e) => draft.setStatus(e.target.value)}
            style={{ margin: '7px 0 14px', fontSize: 13 }}
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

          <div style={{ fontSize: 12, color: 'var(--dim)', marginBottom: 7 }}>
            Ментальные теги
          </div>
          <div
            role="group"
            aria-label="Ментальные теги"
            style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 14 }}
          >
            {Array.from(new Set([...(presets.data?.tags ?? []), ...draft.tags])).map(
              (tag) => (
                <TagChip
                  key={tag}
                  label={tag}
                  on={draft.tags.includes(tag)}
                  onClick={() => draft.toggleTag(tag)}
                />
              ),
            )}
          </div>

          <label style={{ fontSize: 12, color: 'var(--dim)' }} htmlFor="entry-body">
            Состояние
          </label>
          <textarea
            id="entry-body"
            value={draft.note}
            onChange={(e) => draft.setNote(e.target.value)}
            rows={3}
            placeholder="Что происходило с тобой в этот день"
            style={{ width: '100%', marginTop: 7, resize: 'vertical' }}
          />
        </>
      ) : (
        <Locked entry={entry} />
      )}

      {draft.error && (
        <div className="err" style={{ marginTop: 10 }}>
          {draft.error}
        </div>
      )}

      {entry && (!entry.editable || entry.comments.length > 0) && (
        <Comments entry={entry} />
      )}
    </div>
  )
}

export function TagChip({
  label,
  on,
  onClick,
  readonly,
}: {
  label: string
  on: boolean
  onClick?: () => void
  readonly?: boolean
}) {
  return (
    <button
      onClick={onClick}
      disabled={readonly}
      aria-pressed={on}
      style={{
        fontSize: 12,
        padding: '4px 10px',
        borderRadius: 14,
        cursor: readonly ? 'default' : 'pointer',
        background: on ? '#2f3a48' : 'transparent',
        borderColor: on ? 'var(--accent)' : 'var(--line-2)',
        color: on ? 'var(--fg)' : 'var(--dim)',
      }}
    >
      {label}
    </button>
  )
}

function Locked({ entry }: { entry: DiaryEntry | null }) {
  if (!entry) return null
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <Line label="Оценка" value={entry.score ? String(entry.score) : '—'} />
      <Line label="Статус" value={entry.status ?? '—'} />
      <Line label="Теги" value={entry.tags.join(', ') || '—'} />
      {entry.body && (
        <div style={{ whiteSpace: 'pre-wrap', fontSize: 13, marginTop: 4 }}>
          {entry.body}
        </div>
      )}
      <div className="hint">
        Правки закрыты {dateTime(entry.editable_until)}. Запись осталась тем, что ты
        думал тогда — дописать можно комментарием.
      </div>
    </div>
  )
}

function Line({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ display: 'flex', gap: 10 }}>
      <span className="hint" style={{ width: 70, flexShrink: 0 }}>
        {label}
      </span>
      <span style={{ fontSize: 13 }}>{value}</span>
    </div>
  )
}

export function Comments({ entry }: { entry: DiaryEntry }) {
  const qc = useQueryClient()
  const [text, setText] = useState('')
  const [error, setError] = useState('')

  const send = useMutation({
    mutationFn: (body: string) => api.post(`/entries/${entry.id}/comments`, { body }),
    onSuccess: () => {
      setText('')
      setError('')
      qc.invalidateQueries({ queryKey: ['diary'] })
      qc.invalidateQueries({ queryKey: ['today'] })
    },
    onError: (err) =>
      setError(err instanceof ApiError ? err.message : 'Не получилось.'),
  })

  return (
    <div style={{ marginTop: 16, borderTop: '1px solid #232320', paddingTop: 13 }}>
      <div className="klabel" style={{ marginBottom: 9 }}>
        Комментарии
      </div>
      {entry.comments.map((item) => (
        <div key={item.id} style={{ marginBottom: 9 }}>
          <div className="hint">{dateTime(item.created_at)}</div>
          <div style={{ fontSize: 13, whiteSpace: 'pre-wrap' }}>{item.body}</div>
        </div>
      ))}
      <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={2}
          placeholder="Дописать к записи"
          style={{ flex: 1, resize: 'vertical' }}
        />
        <button
          onClick={() => send.mutate(text.trim())}
          disabled={send.isPending || !text.trim()}
          style={{ fontSize: 12, padding: '6px 12px' }}
        >
          Добавить
        </button>
      </div>
      {error && (
        <div className="err" style={{ marginTop: 8 }}>
          {error}
        </div>
      )}
    </div>
  )
}
