import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { ApiError, api } from '../lib/api'
import type { DiaryEntry, DiaryPresets } from '../lib/types'

export function usePresets() {
  return useQuery<DiaryPresets>({
    queryKey: ['diary-presets'],
    queryFn: () => api.get<DiaryPresets>('/diary/presets'),
    staleTime: 60 * 60 * 1000,
  })
}

export type SaveState = 'idle' | 'saving' | 'saved' | 'error'

// Черновик записи с автосохранением. Автосохранение, а не кнопка «Сохранить»,
// по решению прототипа: запись дневника — не форма отчёта, и отдельное действие
// «сохранить» превращает две строки о состоянии в обязанность.
export function useEntryDraft(args: {
  level: 'day' | 'week' | 'month'
  periodStart: string
  entry: DiaryEntry | null
}) {
  const { level, periodStart, entry } = args
  const qc = useQueryClient()

  const [score, setScore] = useState<number | null>(entry?.score ?? null)
  const [status, setStatus] = useState(entry?.status ?? '')
  const [tags, setTags] = useState<string[]>(entry?.tags ?? [])
  const [note, setNote] = useState(entry?.body ?? '')
  const [state, setState] = useState<SaveState>('idle')
  const [error, setError] = useState('')

  // Период мог смениться (выбрали другой день) или запись приехать позже —
  // тогда черновик пересобираем, иначе редактируется не то, что открыто.
  const key = `${level}:${periodStart}:${entry?.id ?? 'new'}`
  const known = useRef(key)
  const dirty = useRef(false)
  useEffect(() => {
    if (known.current === key) return
    known.current = key
    dirty.current = false
    setScore(entry?.score ?? null)
    setStatus(entry?.status ?? '')
    setTags(entry?.tags ?? [])
    setNote(entry?.body ?? '')
    setState('idle')
    setError('')
  }, [key, entry])

  const save = useMutation({
    mutationFn: (body: {
      score: number | null
      status: string | null
      tags: string[]
      body: string | null
    }) => api.put(`/entries/${level}/${periodStart}`, body),
    onMutate: () => setState('saving'),
    onSuccess: () => {
      setState('saved')
      setError('')
      qc.invalidateQueries({ queryKey: ['diary'] })
      qc.invalidateQueries({ queryKey: ['today'] })
    },
    onError: (err) => {
      setState('error')
      setError(err instanceof ApiError ? err.message : 'Не сохранилось.')
    },
  })

  // Пауза перед записью: трейдер печатает, и запрос на каждую букву — это
  // и лишняя нагрузка, и мигающее «сохранено» под руками.
  useEffect(() => {
    if (!dirty.current) return
    const timer = setTimeout(() => {
      save.mutate({
        score,
        status: status.trim() || null,
        tags,
        body: note.trim() || null,
      })
    }, 700)
    return () => clearTimeout(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [score, status, tags, note])

  function touch<T>(setter: (value: T) => void) {
    return (value: T) => {
      dirty.current = true
      setter(value)
    }
  }

  const setTagsTouched = touch(setTags)

  return {
    score,
    setScore: touch(setScore),
    status,
    setStatus: touch(setStatus),
    tags,
    toggleTag: (tag: string) =>
      setTagsTouched(tags.includes(tag) ? tags.filter((t) => t !== tag) : [...tags, tag]),
    addTag: (tag: string) => {
      const clean = tag.trim()
      if (clean && !tags.includes(clean)) setTagsTouched([...tags, clean])
    },
    note,
    setNote: touch(setNote),
    editable: entry === null || entry.editable,
    state,
    error,
    saveHint:
      entry && !entry.editable
        ? 'правки закрыты · остался комментарий'
        : state === 'saving'
          ? 'сохраняю…'
          : state === 'saved'
            ? 'сохранено'
            : state === 'error'
              ? 'не сохранилось'
              : 'сохраняется автоматически',
  }
}
