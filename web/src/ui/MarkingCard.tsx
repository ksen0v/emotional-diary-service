import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../lib/api'
import type { MarkingMetrics, TagsBody } from '../lib/types'
import { money, pct } from './format'

// Экран разметки: что считается нарушением и во что это обходится.
// Две метрики стоят рядом не случайно: 95% дисциплины при покрытии 30%
// означает не дисциплину, а то, что трейдер перестал размечать.
export function MarkingCard() {
  const qc = useQueryClient()

  const tags = useQuery<TagsBody>({
    queryKey: ['tags'],
    queryFn: () => api.get<TagsBody>('/source/tags'),
  })

  const metrics = useQuery<MarkingMetrics>({
    queryKey: ['marking-metrics'],
    queryFn: () => api.get<MarkingMetrics>('/trades/metrics?period=month'),
  })

  const save = useMutation({
    mutationFn: (ids: string[]) =>
      api.put('/source/tags/violations', { violation_tag_ids: ids }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['tags'] })
      // Переразметку делает консьюмер шины, поэтому лента и метрики
      // подтягиваются не мгновенно.
      setTimeout(() => {
        qc.invalidateQueries({ queryKey: ['trades'] })
        qc.invalidateQueries({ queryKey: ['marking-metrics'] })
        qc.invalidateQueries({ queryKey: ['curve'] })
      }, 1400)
    },
  })

  const rows = tags.data?.tags ?? []
  const m = metrics.data?.marking

  function toggle(externalId: string) {
    const next = rows
      .filter((r) => (r.external_id === externalId ? !r.is_violation : r.is_violation))
      .map((r) => r.external_id)
    save.mutate(next)
  }

  return (
    <div className="card" style={{ padding: '18px 20px' }}>
      <div className="klabel" style={{ marginBottom: 14 }}>
        Разметка нарушений
      </div>

      {m && (
        <div style={{ display: 'flex', gap: 26, flexWrap: 'wrap', marginBottom: 16 }}>
          <Metric
            label="Покрытие разметкой"
            value={`${Number(m.coverage_pct).toFixed(0)}%`}
            hint={`${m.marked} из ${m.trades.all} за месяц`}
            color={Number(m.coverage_pct) < 80 ? 'var(--warn)' : 'var(--ok)'}
          />
          <Metric
            label="Коэффициент дисциплины"
            value={m.discipline_pct === null ? '—' : pct(m.discipline_pct, 0)}
            hint={
              m.discipline_pct === null
                ? 'нет размеченных сделок'
                : 'по размеченным сделкам'
            }
          />
          <Metric
            label="Цена эмоций"
            value={money(m.emotion_cost_usd)}
            hint={`${m.violations.count} нарушений`}
            color={Number(m.emotion_cost_usd) < 0 ? 'var(--bad)' : undefined}
          />
          {m.violations.profitable > 0 && (
            <Metric
              label="Нарушения в плюс"
              value={String(m.violations.profitable)}
              hint="остаются нарушениями"
              color="var(--warn)"
            />
          )}
        </div>
      )}

      {!tags.data?.available ? (
        <div className="hint">
          Активный источник не отдаёт теги. Нарушения отмечаются вручную в ленте
          сделок — кнопками в строке.
        </div>
      ) : rows.length === 0 ? (
        <div className="hint">
          Теги появятся здесь, как только придёт первая сделка с тегом. Сервис
          узнаёт их из твоих же сделок, а не просит заводить руками.
        </div>
      ) : (
        <>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            {rows.map((tag) => (
              <label
                key={tag.external_id}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 10,
                  padding: '8px 0',
                  borderBottom: '1px solid #212120',
                  fontSize: 13,
                  cursor: 'pointer',
                }}
              >
                <span style={{ flexGrow: 1 }}>{tag.name}</span>
                <span className="hint">{tag.column_key}</span>
                <input
                  type="checkbox"
                  checked={tag.is_violation}
                  disabled={save.isPending}
                  onChange={() => toggle(tag.external_id)}
                  style={{ accentColor: 'var(--bad)', width: 16, height: 16 }}
                />
                <span
                  style={{
                    width: 92,
                    textAlign: 'right',
                    color: tag.is_violation ? 'var(--bad)' : 'var(--faint)',
                  }}
                >
                  {tag.is_violation ? 'нарушение' : 'не нарушение'}
                </span>
              </label>
            ))}
          </div>
          <div className="hint" style={{ marginTop: 12 }}>
            Что считать нарушением, решаешь только ты: сервис лишь считает
            последствия. Изменение пересчитывает разметку уже принятых сделок,
            поэтому лента обновится через секунду.
          </div>
        </>
      )}
    </div>
  )
}

function Metric({
  label,
  value,
  hint,
  color,
}: {
  label: string
  value: string
  hint: string
  color?: string
}) {
  return (
    <div>
      <div className="klabel" style={{ marginBottom: 4 }}>
        {label}
      </div>
      <div className="mono" style={{ fontSize: 17, color: color ?? 'var(--fg)' }}>
        {value}
      </div>
      <div className="hint">{hint}</div>
    </div>
  )
}
