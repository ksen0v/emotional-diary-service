import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../lib/api'

type TagRow = {
  external_id: string
  name: string
  column_key: string
  is_violation: boolean
}
type TagsBody = { tags: TagRow[]; available: boolean }

// Сокращённая версия экрана разметки: полный вид с покрытием — шаг 3.
// Здесь она нужна, чтобы фильтр «Нарушения» не был мёртвой кнопкой:
// пока ни один тег не отмечен, нарушений в сервисе не существует.
export function TagMapping() {
  const qc = useQueryClient()
  const tags = useQuery<TagsBody>({
    queryKey: ['tags'],
    queryFn: () => api.get<TagsBody>('/source/tags'),
  })

  const save = useMutation({
    mutationFn: (ids: string[]) =>
      api.put('/source/tags/violations', { violation_tag_ids: ids }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['tags'] })
      // Переразметку делает консьюмер шины, поэтому лента обновится
      // не мгновенно — перечитываем её с небольшой задержкой.
      setTimeout(() => {
        qc.invalidateQueries({ queryKey: ['trades'] })
        qc.invalidateQueries({ queryKey: ['curve'] })
      }, 1200)
    },
  })

  if (!tags.data?.available) return null
  const rows = tags.data.tags
  if (rows.length === 0) {
    return (
      <div className="card" style={{ padding: '16px 18px' }}>
        <div className="klabel" style={{ marginBottom: 8 }}>
          Теги разметки
        </div>
        <div className="hint">
          Теги появятся здесь, как только придёт первая сделка с тегом.
        </div>
      </div>
    )
  }

  function toggle(tag: TagRow) {
    const next = rows
      .filter((r) => (r.external_id === tag.external_id ? !r.is_violation : r.is_violation))
      .map((r) => r.external_id)
    save.mutate(next)
  }

  return (
    <div className="card" style={{ padding: '16px 18px' }}>
      <div className="klabel" style={{ marginBottom: 10 }}>
        Какие теги считаются нарушением
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        {rows.map((tag) => (
          <button
            key={tag.external_id}
            onClick={() => toggle(tag)}
            aria-pressed={tag.is_violation}
            disabled={save.isPending}
            style={{
              fontSize: 12,
              padding: '6px 12px',
              borderColor: tag.is_violation ? '#5a3a34' : 'var(--line-2)',
              color: tag.is_violation ? 'var(--bad)' : 'var(--dim)',
            }}
          >
            {tag.name}
            {tag.is_violation ? ' · нарушение' : ''}
          </button>
        ))}
      </div>
      <div className="hint" style={{ marginTop: 10 }}>
        Что считать нарушением, решаешь только ты. Сервис сам этого не решает:
        он лишь считает последствия. Изменение пересчитывает разметку уже
        принятых сделок.
      </div>
    </div>
  )
}
