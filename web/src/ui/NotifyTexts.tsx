import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { ApiError, api } from '../lib/api'
import type { NotifyTemplate, TemplatePreview } from '../lib/types'

// Экран «Тексты уведомлений» из прототипа `NotifyTexts.dc.html`: слева список
// событий с точкой у изменённых, справа поле, чипы подстановок, счётчик до 500,
// «Вернуть стандартный» и карточка «Как придёт».
//
// Два решения, которые видно в коде:
//
// **Предпросмотр рендерит сервер.** В прототипе подстановка сделана на месте,
// но подстановки форматируются по нашим правилам — время в таймзоне трейдера,
// деньги со знаком перед долларом, — и собранный на клиенте предпросмотр
// показывал бы не то, что придёт (Архитектура ч.2 §3.10).
//
// **Сохранение без кнопки.** В прототипе кнопки «Сохранить» нет, и это верно:
// текст здесь правят по одному слову. Сохраняем сами через паузу после
// последнего нажатия, а состояние строки говорит, что происходит.

const SAVE_DELAY_MS = 800
const PREVIEW_DELAY_MS = 250

export function NotifyTexts() {
  const qc = useQueryClient()
  const templates = useQuery<{ templates: NotifyTemplate[] }>({
    queryKey: ['notify-templates'],
    queryFn: () => api.get<{ templates: NotifyTemplate[] }>('/notify/templates'),
  })
  const rows = templates.data?.templates ?? []
  const [selected, setSelected] = useState('lock_started')
  const current = rows.find((t) => t.key === selected) ?? rows[0]

  return (
    <div style={{ display: 'flex', gap: 18, flexGrow: 1, minHeight: 0 }}>
      <div
        className="card"
        style={{
          width: 300,
          flexShrink: 0,
          padding: '12px 10px',
          display: 'flex',
          flexDirection: 'column',
          gap: 2,
          minHeight: 0,
          overflowY: 'auto',
        }}
      >
        <div className="klabel" style={{ padding: '2px 11px 8px' }}>
          События
        </div>
        {rows.map((row) => (
          <button
            key={row.key}
            type="button"
            onClick={() => setSelected(row.key)}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              padding: '9px 11px',
              borderRadius: 8,
              fontSize: 13,
              textAlign: 'left',
              width: '100%',
              border: `1px solid ${row.key === current?.key ? '#3a3a33' : 'transparent'}`,
              background: row.key === current?.key ? '#28281f' : 'transparent',
              color: row.key === current?.key ? 'var(--fg)' : 'var(--dim)',
            }}
          >
            <span>{row.name}</span>
            {row.is_customized && (
              <span
                style={{
                  width: 5,
                  height: 5,
                  borderRadius: '50%',
                  background: 'var(--accent)',
                  marginLeft: 'auto',
                  flexShrink: 0,
                }}
              />
            )}
          </button>
        ))}
      </div>

      {current && (
        <Editor
          key={current.key}
          template={current}
          onSaved={() => qc.invalidateQueries({ queryKey: ['notify-templates'] })}
        />
      )}
    </div>
  )
}

function Editor({
  template,
  onSaved,
}: {
  template: NotifyTemplate
  onSaved: () => void
}) {
  const [body, setBody] = useState(template.body)
  const [error, setError] = useState('')
  const [note, setNote] = useState('')
  const area = useRef<HTMLTextAreaElement | null>(null)
  const dirty = body !== template.body

  const save = useMutation({
    mutationFn: (value: string) =>
      api.put<{ template: NotifyTemplate }>(
        `/notify/templates/${template.key}`,
        { body: value },
      ),
    onSuccess: (data) => {
      setError('')
      setNote(data.template.is_customized ? 'Сохранено' : 'Вернулся стандартный текст')
      onSaved()
    },
    onError: (err) => {
      setNote('')
      setError(err instanceof ApiError ? err.message : 'Не удалось сохранить.')
    },
  })

  const reset = useMutation({
    mutationFn: () =>
      api.del<{ template: NotifyTemplate }>(`/notify/templates/${template.key}`),
    onSuccess: (data) => {
      setBody(data.template.body)
      setError('')
      setNote('Вернулся стандартный текст')
      onSaved()
    },
  })

  // Сохранение через паузу после последнего нажатия. Пока текст не прошёл
  // проверку, он не сохраняется — и причина видна сразу под полем, а не
  // в момент блокировки.
  useEffect(() => {
    if (!dirty) return
    const timer = setTimeout(() => save.mutate(body), SAVE_DELAY_MS)
    return () => clearTimeout(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [body])

  const preview = usePreview(template.key, body)

  function insert(token: string) {
    const field = area.current
    if (!field) {
      setBody(body + token)
      return
    }
    const start = field.selectionStart ?? body.length
    const end = field.selectionEnd ?? body.length
    const next = body.slice(0, start) + token + body.slice(end)
    setBody(next)
    // Курсор остаётся после вставленной подстановки: иначе следующий символ
    // уедет в начало строки, и чип станет неудобнее, чем набор руками.
    requestAnimationFrame(() => {
      field.focus()
      field.setSelectionRange(start + token.length, start + token.length)
    })
  }

  const tooLong = body.length > template.max_length

  return (
    <div
      style={{
        flexGrow: 1,
        display: 'flex',
        flexDirection: 'column',
        gap: 14,
        minWidth: 0,
        minHeight: 0,
        overflowY: 'auto',
      }}
    >
      <div className="card" style={{ padding: '16px 18px' }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
          <span className="klabel">{template.name}</span>
          <span className="hint">{template.when}</span>
          <div style={{ flexGrow: 1 }} />
          <span
            className="mono"
            style={{ fontSize: 11, color: tooLong ? 'var(--bad)' : 'var(--faint)' }}
          >
            {body.length} / {template.max_length}
          </span>
        </div>

        <div style={{ marginTop: 11 }}>
          <label htmlFor="nt-body" className="klabel" style={{ display: 'block', marginBottom: 6 }}>
            Текст
          </label>
          <textarea
            id="nt-body"
            ref={area}
            value={body}
            onChange={(e) => setBody(e.target.value)}
            placeholder="Текст уведомления"
            style={{ width: '100%', minHeight: 104, resize: 'vertical', fontSize: 14 }}
          />
        </div>

        <div style={{ marginTop: 12 }}>
          <div className="klabel" style={{ marginBottom: 7 }}>
            Подстановки — вставить кликом
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {template.placeholders.map((name) => (
              <button
                key={name}
                type="button"
                className="mono"
                onClick={() => insert(`{${name}}`)}
                style={{ fontSize: 11, padding: '4px 9px', borderRadius: 12 }}
              >
                {`{${name}}`}
              </button>
            ))}
          </div>
        </div>

        <div style={{ marginTop: 14, display: 'flex', alignItems: 'center', gap: 10 }}>
          <button
            type="button"
            disabled={!template.is_customized}
            onClick={() => reset.mutate()}
            style={{ fontSize: 12, padding: '7px 13px' }}
          >
            Вернуть стандартный
          </button>
          <span className="hint">
            {error
              ? ''
              : save.isPending
                ? 'Сохраняю…'
                : note || (template.is_customized ? 'Текст изменён' : 'Стандартный текст')}
          </span>
        </div>

        {error && (
          <div className="err" role="alert" style={{ marginTop: 10 }}>
            {error}
          </div>
        )}
      </div>

      <div className="card" style={{ padding: '16px 18px', background: '#1a1a18' }}>
        <div className="klabel" style={{ marginBottom: 9 }}>
          Как придёт
        </div>
        <div style={{ display: 'flex', gap: 11, alignItems: 'flex-start' }}>
          <span
            style={{
              width: 26,
              height: 26,
              borderRadius: 7,
              background: 'var(--panel-2)',
              border: '1px solid var(--line)',
              flexShrink: 0,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: 11,
              color: 'var(--faint)',
            }}
          >
            {template.buddy ? 'Д' : 'Я'}
          </span>
          <div style={{ flexGrow: 1, minWidth: 0 }}>
            <div style={{ fontSize: 14, lineHeight: 1.55 }}>
              {preview ?? template.preview}
            </div>
            <div className="hint" style={{ marginTop: 6 }}>
              {template.to} · пример данных
            </div>
          </div>
        </div>
      </div>

      <div className="hint">
        {template.buddy
          ? 'Суммы в сообщениях доверенному лицу недоступны: друг видит факт, а не финансы. Персональный текст для конкретного контакта задаётся в карточке Telegram и перебивает этот.'
          : 'Что написано, то и придёт. Сервис проверяет только подстановки и длину — на осмысленность текст не проверяется и на стандартный не заменяется.'}
      </div>
    </div>
  )
}

function usePreview(key: string, body: string): string | null {
  const [debounced, setDebounced] = useState(body)
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(body), PREVIEW_DELAY_MS)
    return () => clearTimeout(timer)
  }, [body])

  const preview = useQuery<TemplatePreview>({
    queryKey: ['notify-preview', key, debounced],
    queryFn: () =>
      api.post<TemplatePreview>(`/notify/templates/${key}/preview`, {
        body: debounced,
      }),
    // Пока текст не проходит проверку, предпросмотра нет — и это честно:
    // показывать старую строку под новым текстом значит врать.
    retry: false,
    enabled: debounced.trim().length > 0,
  })
  if (preview.isError) return null
  return preview.data?.rendered ?? null
}
