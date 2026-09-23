import { useQuery } from '@tanstack/react-query'
import { api } from '../lib/api'
import type { IncidentsList } from '../lib/types'
import { IncidentRow, OUTCOME } from '../ui/IncidentRow'

// Экран «Инциденты» из прототипа Incidents.dc.html: сводная полоса сверху,
// под ней лента с цветной планкой слева и чипом исхода справа.
//
// Сводка, заголовок строки и подпись под ним приходят с сервера готовыми.
// Собирать их здесь было бы короче, но те же строки стоят в блоке «Инциденты
// сегодня» на «Сегодня» и уйдут в уведомление — три сборщика однажды опишут
// одно событие тремя способами.
//
// Отступления от прототипа, все названы:
//  1. Третий исход — «идёт». В прототипе исхода два, но инцидент, у которого
//     блокировка ещё не кончилась, не соблюдён и не нарушен, и красить его
//     в любой из двух цветов значило бы сказать неправду.
//  2. Коэффициента дисциплины в полосе прототипа нет — там только счётчики.
//     Число сервер считает и отдаёт, на экране его нет: собираем по прототипу.
//  3. Фильтр по исходу — в контракте (ч.2 §3.7), в прототипе его нет.
//     Поэтому по умолчанию он «все», и это ровно то, что нарисовано.

export function IncidentsPage() {
  const list = useQuery<IncidentsList>({
    queryKey: ['incidents'],
    queryFn: () => api.get('/incidents'),
  })

  if (list.isLoading) {
    return <div style={{ color: 'var(--faint)' }}>загрузка…</div>
  }
  if (list.isError || !list.data) {
    return <div className="err">Инциденты не загрузились. Обнови страницу.</div>
  }

  const { items, totals, period } = list.data

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16, flexGrow: 1, minHeight: 0 }}>
      <div
        className="card"
        style={{ padding: '13px 18px', display: 'flex', alignItems: 'center', gap: 30 }}
      >
        <Count label={period.label} value={totals.count} />
        <Count label="Соблюдено" value={totals.kept} color={OUTCOME.kept.fg} />
        <Count label="Нарушено" value={totals.breached} color={OUTCOME.breached.fg} />
        {totals.open > 0 && (
          <Count label="Идёт" value={totals.open} color={OUTCOME.open.fg} />
        )}
        <div style={{ flexGrow: 1 }} />
        {/* Надпись из прототипа дословно. Это не украшение: она объясняет,
            почему у строк нет ни кнопки правки, ни корзины (ТЗ 9.2). */}
        <span className="mono" style={{ fontSize: 12, color: 'var(--faint)' }}>
          Инциденты не удаляются и не редактируются
        </span>
      </div>

      <div
        className="card"
        style={{
          padding: '8px 18px 16px',
          flexGrow: 1,
          minHeight: 0,
          overflowY: 'auto',
        }}
      >
        {items.length === 0 ? (
          <div className="hint" style={{ padding: '15px 0' }}>
            Инцидентов нет. Здесь появятся срабатывания правил и системных
            триггеров — с исходом «соблюдено» или «нарушено».
          </div>
        ) : (
          items.map((item) => <IncidentRow key={item.id} item={item} />)
        )}
      </div>
    </div>
  )
}

function Count({
  label,
  value,
  color,
}: {
  label: string
  value: number
  color?: string
}) {
  return (
    <span style={{ fontSize: 13, color: 'var(--dim)' }}>
      {label}{' '}
      <span className="mono" style={{ color: color ?? 'var(--fg)' }}>
        {value}
      </span>
    </span>
  )
}
