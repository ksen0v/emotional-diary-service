import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { ApiError, api } from '../lib/api'
import type { DiaryEntry, DiaryPresets } from '../lib/types'
import { dateTime } from './format'

// Запись дневника: оценка, статус, ментальные теги, текст. После 48 часов
// поля закрываются и остаётся комментарий — запись должна остаться тем, что
// трейдер думал тогда, а не тем, что он думает об этом сейчас (ТЗ 9.2).
export function EntryEditor({
  level,
  periodStart,
  entry,
  onSaved,
}: {
  level: 'day' | 'week' | 'month'
  periodStart: string
  entry: DiaryEntry | null
  onSaved?: () => void
}) {
  const qc = useQueryClient()
  const presets = useQuery<DiaryPresets>({
    queryKey: ['diary-presets'],
    queryFn: () => api.get<DiaryPresets>('/diary/presets'),
    staleTime: 60 * 60 * 1000,
  })

  const [score, setScore] = useState<number | null>(entry?.score ?? null)
  const [status, setStatus] = useState(entry?.status ?? '')
  const [tags, setTags] = useState<string[]>(entry?.tags ?? [])
  const [body, setBody] = useState(entry?.body ?? '')
  const [ownTag, setOwnTag] = useState('')
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)

  // Запись могла прийти позже монтирования (или смениться при выборе другого
  // дня) — тогда поля надо пересобрать, иначе редактируется не то, что открыто.
  useEffect(() => {
    setScore(entry?.score ?? null)
    setStatus(entry?.status ?? '')
    setTags(entry?.tags ?? [])
    setBody(entry?.body ?? '')
    setSaved(false)
    setError('')
  }, [entry?.id, periodStart])

  const editable = entry === null || entry.editable

  const save = useMutation({
    mutationFn: () =>
      api.put(`/entries/${level}/${periodStart}`, {
        score,
        status: status || null,
        tags,
        body: body || null,
      }),
    onSuccess: () => {
      setError('')
      setSaved(true)
      qc.invalidateQueries({ queryKey: ['diary'] })
      qc.invalidateQueries({ queryKey: ['today'] })
      onSaved?.()
    },
    onError: (err) => {
      setSaved(false)
      setError(err instanceof ApiError ? err.message : 'Не получилось сохранить.')
    },
  })

  const comment = useMutation({
    mutationFn: (text: string) =>
      api.post(`/entries/${entry?.id}/comments`, { body: text }),
    onSuccess: () => {
      setOwnTag('')
      qc.invalidateQueries({ queryKey: ['diary'] })
      qc.invalidateQueries({ queryKey: ['today'] })
    },
    onError: (err) =>
      setError(err instanceof ApiError ? err.message : 'Не получилось.'),
  })

  function toggleTag(tag: string) {
    setTags(tags.includes(tag) ? tags.filter((t) => t !== tag) : [...tags, tag])
  }

  return (
    <div className="card" style={{ padding: '18px 20px' }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'baseline',
          gap: 10,
          marginBottom: 14,
          flexWrap: 'wrap',
        }}
      >
        <div className="klabel">Запись</div>
        {!editable && (
          <span className="hint">
            правки закрыты {dateTime(entry?.editable_until ?? null)} — остался комментарий
          </span>
        )}
      </div>

      {editable ? (
        <>
          <Field label="Оценка периода">
            <div style={{ display: 'flex', gap: 6 }}>
              {[1, 2, 3, 4, 5].map((value) => (
                <button
                  key={value}
                  onClick={() => setScore(score === value ? null : value)}
                  className={score === value ? 'primary' : ''}
                  style={{ width: 40, fontSize: 14, padding: '8px 0' }}
                >
                  {value}
                </button>
              ))}
            </div>
          </Field>

          <Field label="Статус">
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 8 }}>
              {(presets.data?.statuses ?? []).map((preset) => (
                <Chip
                  key={preset}
                  label={preset}
                  on={status === preset}
                  onClick={() => setStatus(status === preset ? '' : preset)}
                />
              ))}
            </div>
            <input
              value={status}
              onChange={(e) => setStatus(e.target.value)}
              placeholder="или своими словами"
              style={{ width: '100%', maxWidth: 360 }}
            />
          </Field>

          <Field label="Ментальные теги">
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 8 }}>
              {Array.from(new Set([...(presets.data?.tags ?? []), ...tags])).map((tag) => (
                <Chip
                  key={tag}
                  label={tag}
                  on={tags.includes(tag)}
                  onClick={() => toggleTag(tag)}
                />
              ))}
            </div>
            <div style={{ display: 'flex', gap: 6 }}>
              <input
                value={ownTag}
                onChange={(e) => setOwnTag(e.target.value)}
                placeholder="свой тег"
                style={{ maxWidth: 200 }}
              />
              <button
                onClick={() => {
                  const tag = ownTag.trim()
                  if (tag && !tags.includes(tag)) setTags([...tags, tag])
                  setOwnTag('')
                }}
                style={{ fontSize: 12, padding: '6px 12px' }}
              >
                Добавить
              </button>
            </div>
          </Field>

          <Field label="Что было">
            <textarea
              value={body}
              onChange={(e) => setBody(e.target.value)}
              rows={4}
              placeholder="Своими словами, без отчёта."
              style={{ width: '100%', resize: 'vertical' }}
            />
          </Field>

          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <button
              className="primary"
              onClick={() => save.mutate()}
              disabled={save.isPending}
            >
              {save.isPending ? 'Сохраняю…' : 'Сохранить'}
            </button>
            {saved && <span className="ok-text">сохранено</span>}
          </div>
        </>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          <Readonly label="Оценка" value={entry?.score ? String(entry.score) : '—'} />
          <Readonly label="Статус" value={entry?.status ?? '—'} />
          <Readonly label="Теги" value={entry?.tags.join(', ') || '—'} />
          <div style={{ whiteSpace: 'pre-wrap', fontSize: 13 }}>{entry?.body}</div>
        </div>
      )}

      {entry && (
        <div style={{ marginTop: 18, borderTop: '1px solid #2b2b27', paddingTop: 14 }}>
          <div className="klabel" style={{ marginBottom: 10 }}>
            Комментарии
          </div>
          {entry.comments.length === 0 && (
            <div className="hint" style={{ marginBottom: 10 }}>
              Комментарий можно дописать когда угодно — он не переписывает запись.
            </div>
          )}
          {entry.comments.map((item) => (
            <div key={item.id} style={{ marginBottom: 10 }}>
              <div className="hint">{dateTime(item.created_at)}</div>
              <div style={{ fontSize: 13, whiteSpace: 'pre-wrap' }}>{item.body}</div>
            </div>
          ))}
          <CommentBox onSend={(text) => comment.mutate(text)} busy={comment.isPending} />
        </div>
      )}

      {error && (
        <div className="err" style={{ marginTop: 12 }}>
          {error}
        </div>
      )}
    </div>
  )
}

function CommentBox({
  onSend,
  busy,
}: {
  onSend: (text: string) => void
  busy: boolean
}) {
  const [text, setText] = useState('')
  return (
    <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={2}
        placeholder="Дописать к записи"
        style={{ flex: 1, resize: 'vertical' }}
      />
      <button
        onClick={() => {
          if (text.trim()) {
            onSend(text.trim())
            setText('')
          }
        }}
        disabled={busy || !text.trim()}
        style={{ fontSize: 12, padding: '6px 12px' }}
      >
        Добавить
      </button>
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 16 }}>
      <div className="klabel" style={{ marginBottom: 8 }}>
        {label}
      </div>
      {children}
    </div>
  )
}

function Chip({
  label,
  on,
  onClick,
}: {
  label: string
  on: boolean
  onClick: () => void
}) {
  return (
    <button
      onClick={onClick}
      style={{
        fontSize: 12,
        padding: '5px 11px',
        borderRadius: 14,
        color: on ? 'var(--fg)' : 'var(--dim)',
        borderColor: on ? 'var(--fg)' : 'var(--line-2)',
      }}
    >
      {label}
    </button>
  )
}

function Readonly({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ display: 'flex', gap: 10 }}>
      <span className="hint" style={{ width: 80 }}>
        {label}
      </span>
      <span style={{ fontSize: 13 }}>{value}</span>
    </div>
  )
}
