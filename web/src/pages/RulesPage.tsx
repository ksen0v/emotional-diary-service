import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { ApiError, api } from '../lib/api'
import type {
  Rule,
  RuleCondition,
  RuleMetric,
  RulePreview,
  RuleUnlock,
  RulesDictionary,
  RulesList,
} from '../lib/types'
import { plural } from '../ui/format'

// Экран «Правила» из прототипа RuleBuilder.dc.html: список слева, конструктор
// справа, «Правило словами» под ним.
//
// Фразу считает сервер — и сохранённую, и черновую (POST /rules/preview).
// Собирать её здесь было бы проще, но та же строка уйдёт в Telegram и в запись
// инцидента, и три сборщика однажды опишут одно правило тремя способами.
//
// Отступления от прототипа, все вынужденные:
//  1. Название правила — поле ввода, а не заголовок: иначе новое правило не назвать.
//  2. Чип «включено» кликается, и у своих правил есть «Удалить»: без них
//     выключение и удаление правила остались бы только в API.
//  3. У системного триггера блок «Если» — текст, а не селекты: его условие
//     тремя показателями не выражается и живёт в коде (Архитектура ч.1 §7).
//  4. Карточек системных правил четыре, а в прототипе три: ТЗ 6.5 описывает
//     SR-1…SR-4, и спрятать четвёртый значило бы спрятать триггер, который
//     будет срабатывать.
//  5. Доверенное лицо названо «доверенным лицом», а не «Максимом»: контактов
//     ещё нет, и тумблеры про них выключены с объяснением.

type Draft = {
  id: string | null
  systemCode: string | null
  name: string
  enabled: boolean
  conditions: RuleCondition[]
  alert: boolean
  lockOn: boolean
  minutes: string
  buddy: boolean
  unlock: RuleUnlock
  remindAfter: string | null
  editable: string[] | null
  fired: number
  ifText: string
}

const UNLOCK_NOTE: Record<string, string> = {
  review: 'три вопроса о случившемся',
  buddy: 'кнопка в Telegram',
}

function draftOf(rule: Rule): Draft {
  return {
    id: rule.id,
    systemCode: rule.system_code,
    name: rule.name,
    enabled: rule.enabled,
    conditions: rule.conditions?.items.map((c) => ({ ...c })) ?? [],
    alert: rule.actions.alert,
    lockOn: rule.actions.lock.enabled,
    minutes: rule.actions.lock.minutes === null ? '' : String(rule.actions.lock.minutes),
    buddy: rule.actions.buddy,
    unlock: { ...rule.unlock },
    remindAfter:
      rule.actions.remind_after_minutes === undefined
        ? null
        : String(rule.actions.remind_after_minutes),
    editable: rule.editable_fields,
    fired: rule.fired_last_30d,
    ifText: rule.if_text,
  }
}

// Значения по умолчанию взяты из прототипа: новое правило открывается на
// «2 стопа подряд», добавленное условие — на «просадка от пика дня 5%».
// Пустая заготовка с минимальными границами заставляла бы вводить оба числа.
const DEFAULTS: Record<string, string> = {
  loss_streak: '2',
  drawdown_pct: '5',
  loss_sum_pct: '3',
}

function defaultCondition(metric: RuleMetric): RuleCondition {
  return { metric: metric.key, cmp: 'ge', value: DEFAULTS[metric.key] ?? metric.min }
}

function pick(metrics: RuleMetric[], key: string): RuleMetric | undefined {
  return metrics.find((m) => m.key === key) ?? metrics[0]
}

function blankDraft(metrics: RuleMetric[]): Draft {
  const first = pick(metrics, 'loss_streak')
  return {
    id: null,
    systemCode: null,
    name: 'Новое правило',
    enabled: true,
    conditions: first ? [defaultCondition(first)] : [],
    alert: true,
    lockOn: true,
    minutes: '30',
    buddy: false,
    unlock: { timer: true, review: true, buddy: false },
    remindAfter: null,
    editable: null,
    fired: 0,
    ifText: '',
  }
}

function can(draft: Draft, field: string): boolean {
  if (draft.editable === null) return true
  return draft.editable.includes(field)
}

export function RulesPage() {
  const queryClient = useQueryClient()
  const dict = useQuery<RulesDictionary>({
    queryKey: ['rules', 'metrics'],
    queryFn: () => api.get('/rules/metrics'),
  })
  const list = useQuery<RulesList>({
    queryKey: ['rules', 'list'],
    queryFn: () => api.get('/rules'),
  })

  const [draft, setDraft] = useState<Draft | null>(null)
  const [saveError, setSaveError] = useState<string | null>(null)

  // Первое открытие: выбирается своё правило, а если своих нет — первый
  // системный триггер. Пустой правый столбец на экране, где всегда есть что
  // показать, читался бы как поломка.
  useEffect(() => {
    if (draft !== null || !list.data) return
    const rules = list.data.rules
    const chosen = rules.find((r) => r.kind === 'user') ?? rules[0]
    if (chosen) setDraft(draftOf(chosen))
  }, [list.data, draft])

  const body = useMemo(() => (draft ? previewBody(draft) : null), [draft])
  const [settled, setSettled] = useState<string | null>(null)
  useEffect(() => {
    const key = body ? JSON.stringify(body) : null
    const timer = setTimeout(() => setSettled(key), 200)
    return () => clearTimeout(timer)
  }, [body])

  const preview = useQuery<RulePreview>({
    queryKey: ['rules', 'preview', settled],
    queryFn: () => api.post('/rules/preview', JSON.parse(settled as string)),
    enabled: settled !== null,
    placeholderData: (prev) => prev,
  })

  const save = useMutation({
    mutationFn: async () => {
      if (!draft) return null
      if (draft.id === null) {
        return api.post<Rule>('/rules', { name: draft.name, ...previewBody(draft) })
      }
      return api.patch<Rule>(`/rules/${draft.id}`, patchBody(draft))
    },
    onMutate: () => setSaveError(null),
    onSuccess: (rule) => {
      if (rule) setDraft(draftOf(rule))
      queryClient.invalidateQueries({ queryKey: ['rules'] })
    },
    onError: (err) =>
      setSaveError(err instanceof ApiError ? err.message : 'Сохранить не получилось.'),
  })

  const toggle = useMutation({
    mutationFn: (next: boolean) =>
      api.post<Rule>(`/rules/${draft?.id}/toggle`, { enabled: next }),
    onSuccess: (rule) => {
      setDraft(draftOf(rule))
      queryClient.invalidateQueries({ queryKey: ['rules'] })
    },
    onError: (err) =>
      setSaveError(err instanceof ApiError ? err.message : 'Не получилось.'),
  })

  const remove = useMutation({
    mutationFn: () => api.del(`/rules/${draft?.id}`),
    onSuccess: () => {
      setDraft(null)
      queryClient.invalidateQueries({ queryKey: ['rules'] })
    },
    onError: (err) =>
      setSaveError(err instanceof ApiError ? err.message : 'Удалить не получилось.'),
  })

  if (dict.isLoading || list.isLoading) {
    return <div style={{ color: 'var(--faint)' }}>загрузка…</div>
  }
  if (dict.isError || list.isError || !dict.data || !list.data) {
    return <div className="err">Правила не загрузились. Обнови страницу.</div>
  }

  const rules = list.data.rules
  const system = rules.filter((r) => r.kind === 'system')
  const mine = rules.filter((r) => r.kind === 'user')

  function patch(next: Partial<Draft>) {
    setDraft((current) => (current ? { ...current, ...next } : current))
    setSaveError(null)
  }

  return (
    <div style={{ display: 'flex', gap: 20, flexGrow: 1, minHeight: 0 }}>
      <div
        style={{
          width: 280,
          flexShrink: 0,
          display: 'flex',
          flexDirection: 'column',
          gap: 10,
          overflowY: 'auto',
        }}
      >
        <div className="klabel">Системные · отключить нельзя</div>
        {system.map((rule) => (
          <RuleCard
            key={rule.id}
            rule={rule}
            selected={draft?.id === rule.id}
            onSelect={() => setDraft(draftOf(rule))}
          />
        ))}

        <div className="klabel" style={{ marginTop: 10 }}>
          Мои правила
        </div>
        {mine.length === 0 && (
          <div className="hint">Своих правил пока нет. Собери первое.</div>
        )}
        {mine.map((rule) => (
          <RuleCard
            key={rule.id}
            rule={rule}
            selected={draft?.id === rule.id}
            onSelect={() => setDraft(draftOf(rule))}
          />
        ))}
        <button
          onClick={() => setDraft(blankDraft(dict.data.metrics))}
          style={{
            marginTop: 4,
            padding: 10,
            borderRadius: 8,
            border: '1px dashed #3c3c36',
            fontSize: 13,
            background: draft?.id === null && draft !== null ? '#1e2228' : 'transparent',
          }}
        >
          + Новое правило
        </button>
      </div>

      {draft === null ? (
        <div className="card" style={{ flexGrow: 1, padding: '20px 22px' }}>
          <div className="hint">Выбери правило слева или собери новое.</div>
        </div>
      ) : (
        <Builder
          draft={draft}
          dict={dict.data}
          engine={list.data.engine}
          preview={preview.data}
          patch={patch}
          onSave={() => save.mutate()}
          saving={save.isPending}
          saveError={saveError}
          onToggle={(next) => toggle.mutate(next)}
          onDelete={() => remove.mutate()}
          removing={remove.isPending}
        />
      )}
    </div>
  )
}

// --- левая колонка ---

function RuleCard({
  rule,
  selected,
  onSelect,
}: {
  rule: Rule
  selected: boolean
  onSelect: () => void
}) {
  const title = rule.system_code ? `${rule.system_code} ${rule.name}` : rule.name
  return (
    <button className={selected ? 'rulecard on' : 'rulecard'} onClick={onSelect}>
      <div style={{ fontSize: 13, fontWeight: 500 }}>{title}</div>
      <div style={{ fontSize: 12, color: 'var(--faint)', marginTop: 5 }}>
        {rule.summary} · {rule.fired_last_30d}{' '}
        {plural(rule.fired_last_30d, 'срабатывание', 'срабатывания', 'срабатываний')}
        {!rule.enabled && ' · выключено'}
      </div>
      <div style={{ display: 'flex', gap: 5, marginTop: 7, flexWrap: 'wrap' }}>
        {rule.actions.lock.enabled ? (
          <>
            {rule.unlock.timer && <span className="chip">таймер</span>}
            {rule.unlock.review && <span className="chip">разбор</span>}
            {rule.unlock.buddy && <span className="chip">доверенное лицо</span>}
            {!rule.unlock.timer && !rule.unlock.review && !rule.unlock.buddy && (
              <span className="chip" style={{ color: 'var(--warn)' }}>
                до границы дня
              </span>
            )}
          </>
        ) : (
          <span className="chip" style={{ color: '#6e6c65' }}>
            без блокировки
          </span>
        )}
      </div>
    </button>
  )
}

// --- правая колонка ---

function Builder({
  draft,
  dict,
  engine,
  preview,
  patch,
  onSave,
  saving,
  saveError,
  onToggle,
  onDelete,
  removing,
}: {
  draft: Draft
  dict: RulesDictionary
  engine: { active: boolean; note: string }
  preview: RulePreview | undefined
  patch: (next: Partial<Draft>) => void
  onSave: () => void
  saving: boolean
  saveError: string | null
  onToggle: (next: boolean) => void
  onDelete: () => void
  removing: boolean
}) {
  const isSystem = draft.systemCode !== null
  const sentence = preview?.human_text ?? ''
  const problem = preview?.problem ?? null
  const empty = !draft.alert && !draft.lockOn && !draft.buddy
  const nameEmpty = draft.name.trim().length === 0
  const canSave = Boolean(preview?.valid) && !nameEmpty

  return (
    <div
      className="card"
      style={{
        flexGrow: 1,
        padding: '20px 22px',
        display: 'flex',
        flexDirection: 'column',
        gap: 16,
        minWidth: 0,
        overflowY: 'auto',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        {isSystem && (
          <span className="serif" style={{ fontSize: 24, color: 'var(--faint)' }}>
            {draft.systemCode}
          </span>
        )}
        <input
          className="rulename"
          value={draft.name}
          disabled={isSystem}
          onChange={(e) => patch({ name: e.target.value })}
          aria-label="Название правила"
          style={{ flexGrow: 1 }}
        />
        {isSystem ? (
          <span className="pill on" title="Системный триггер отключить нельзя">
            включено
          </span>
        ) : (
          <button
            className={draft.enabled ? 'pill on' : 'pill'}
            disabled={draft.id === null}
            onClick={() => onToggle(!draft.enabled)}
            title={draft.id === null ? 'Сначала сохрани правило' : 'Включить или выключить'}
          >
            {draft.enabled ? 'включено' : 'выключено'}
          </button>
        )}
      </div>

      <Note>
        Правило проверяется сразу, как приходят данные: на каждой новой закрытой сделке
        и в момент, когда в дневнике появляется тег. Настраивать момент проверки не нужно.
      </Note>

      {!engine.active && <Note tone="warn">{engine.note}</Note>}

      <Row label="Если">
        {isSystem ? (
          <div style={{ fontSize: 13, color: 'var(--fg)' }}>
            {capitalize(draft.ifText)}.
            <div className="hint" style={{ marginTop: 6 }}>
              Условие системного триггера задано в сервисе: тремя показателями оно
              не выражается, поэтому здесь его не собрать и не отключить.
            </div>
          </div>
        ) : (
          <Conditions draft={draft} dict={dict} patch={patch} />
        )}

        <div
          style={{
            display: 'flex',
            flexDirection: 'column',
            gap: 6,
            paddingTop: 6,
            marginTop: 2,
            borderTop: '1px solid #232320',
          }}
        >
          <span className="hint">
            Всё считается внутри торгового дня. На границе дня счётчики обнуляются:
            серия, просадка и суммарный убыток начинаются заново.
          </span>
          <span className="hint">
            Мелкие сделки в счётчиках не участвуют: значимой считается сделка с убытком
            от <span className="mono" style={{ color: '#dad7d0' }}>{dict.significance_pct}%</span>{' '}
            депозита. Порог общий для всех правил, меняется в{' '}
            <Link to="/settings">Настройках</Link>.
          </span>
          {dict.unavailable_metrics.length > 0 && !isSystem && (
            <span className="hint">
              {dict.unavailable_metrics.map((m) => m.name).join(', ')} —{' '}
              {dict.unavailable_metrics[0].reason} Эти показатели в списке затенены.
            </span>
          )}
        </div>
      </Row>

      <Row label="То">
        <Toggle
          label="Алерт в Telegram"
          on={draft.alert}
          disabled={!can(draft, 'actions.alert')}
          onClick={() => patch({ alert: !draft.alert })}
        />

        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 13, color: draft.lockOn ? 'var(--fg)' : 'var(--faint)' }}>
            Блокировка торговли
          </span>
          <input
            className="num"
            value={draft.minutes}
            disabled={!draft.lockOn || !can(draft, 'actions.lock.minutes')}
            onChange={(e) => patch({ minutes: e.target.value.replace(/[^\d]/g, '') })}
            aria-label="Длительность блокировки в минутах"
            placeholder={isSystem ? '—' : ''}
          />
          <span style={{ fontSize: 13, color: 'var(--faint)' }}>мин</span>
          <div style={{ marginLeft: 'auto' }}>
            <Pill
              on={draft.lockOn}
              disabled={!can(draft, 'actions.lock.enabled')}
              onClick={() => patch({ lockOn: !draft.lockOn })}
            />
          </div>
        </div>
        {isSystem && draft.minutes === '' && (
          <span className="hint" style={{ marginLeft: 14 }}>
            Пусто — блокировка держится до границы торгового дня.
          </span>
        )}

        <div
          style={{
            marginLeft: 14,
            paddingLeft: 14,
            borderLeft: '1px solid var(--line)',
            display: 'flex',
            flexDirection: 'column',
            gap: 8,
            opacity: draft.lockOn ? 1 : 0.4,
          }}
        >
          <div className="klabel">Условия снятия</div>
          {dict.unlock_conditions.map((cond) => {
            const on = draft.unlock[cond.key]
            const needsContact = Boolean(cond.requires_contact) && !dict.buddy_available
            const note =
              cond.key === 'timer'
                ? draft.minutes === ''
                  ? 'до границы дня'
                  : `выждать ${draft.minutes} ${plural(Number(draft.minutes), 'минуту', 'минуты', 'минут')}`
                : needsContact
                  ? dict.buddy_note
                  : UNLOCK_NOTE[cond.key]
            return (
              <div
                key={cond.key}
                style={{ display: 'flex', alignItems: 'center', gap: 10 }}
              >
                <span
                  style={{
                    fontSize: 13,
                    color: on ? 'var(--fg)' : 'var(--faint)',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {cond.name}
                </span>
                <span style={{ fontSize: 12, color: '#6e6c65' }}>{note}</span>
                <div style={{ marginLeft: 'auto' }}>
                  <Pill
                    on={on}
                    disabled={!draft.lockOn || needsContact || !can(draft, 'unlock')}
                    onClick={() =>
                      patch({ unlock: { ...draft.unlock, [cond.key]: !on } })
                    }
                  />
                </div>
              </div>
            )
          })}
          {draft.lockOn &&
            !draft.unlock.timer &&
            !draft.unlock.review &&
            !draft.unlock.buddy && (
              <div style={{ fontSize: 12, color: 'var(--warn)', lineHeight: 1.5 }}>
                Ни одно условие не выбрано — блокировку нельзя будет снять до границы дня.
              </div>
            )}
        </div>

        <Toggle
          label="Сигнал доверенному лицу"
          on={draft.buddy}
          disabled={!dict.buddy_available || !can(draft, 'actions.buddy')}
          note={dict.buddy_available ? undefined : dict.buddy_note}
          onClick={() => patch({ buddy: !draft.buddy })}
        />

        {draft.remindAfter !== null && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <span style={{ fontSize: 13 }}>Напомнить через</span>
            <input
              className="num"
              value={draft.remindAfter}
              disabled={!can(draft, 'actions.remind_after_minutes')}
              onChange={(e) =>
                patch({ remindAfter: e.target.value.replace(/[^\d]/g, '') })
              }
              aria-label="Через сколько минут напомнить о разметке"
            />
            <span style={{ fontSize: 13, color: 'var(--faint)' }}>мин после закрытия</span>
          </div>
        )}
      </Row>

      <div
        style={{
          padding: '16px 18px',
          borderRadius: 9,
          background: empty ? '#2a2418' : '#22221e',
          border: `1px solid ${empty ? '#4a3a20' : 'var(--line-2)'}`,
        }}
      >
        <div className="klabel" style={{ marginBottom: 9 }}>
          Правило словами
        </div>
        <div
          style={{
            fontSize: 14,
            lineHeight: 1.65,
            color: empty ? 'var(--warn)' : 'var(--fg)',
          }}
        >
          {sentence || '…'}
        </div>
      </div>

      {problem && !empty && (
        <div className="err">{problem.message}</div>
      )}
      {nameEmpty && <div className="err">У правила должно быть название.</div>}
      {saveError && <div className="err">{saveError}</div>}

      <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
          <span className="mono" style={{ fontSize: 22 }}>
            {draft.fired}
          </span>
          <span style={{ fontSize: 13, color: 'var(--dim)' }}>
            {plural(draft.fired, 'срабатывание', 'срабатывания', 'срабатываний')} за
            последние 30 дней
          </span>
        </div>
        <div style={{ flexGrow: 1 }} />
        {!isSystem && draft.id !== null && (
          <button onClick={onDelete} disabled={removing} style={{ fontSize: 13 }}>
            {removing ? 'удаляю…' : 'Удалить'}
          </button>
        )}
        <button
          className="cta"
          disabled={!canSave || saving}
          onClick={onSave}
          style={{ padding: '10px 20px' }}
        >
          {saving ? 'сохраняю…' : 'Сохранить'}
        </button>
      </div>
    </div>
  )
}

function Conditions({
  draft,
  dict,
  patch,
}: {
  draft: Draft
  dict: RulesDictionary
  patch: (next: Partial<Draft>) => void
}) {
  const many = draft.conditions.length > 1
  const full = draft.conditions.length >= dict.max_conditions

  function change(index: number, next: Partial<RuleCondition>) {
    const items = draft.conditions.map((c, i) => (i === index ? { ...c, ...next } : c))
    patch({ conditions: items })
  }

  function unit(metricKey: string): string {
    return dict.metrics.find((m) => m.key === metricKey)?.unit ?? ''
  }

  return (
    <>
      {draft.conditions.map((cond, i) => (
        <div
          key={i}
          style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'nowrap' }}
        >
          {i === 0 && many && <span style={{ width: 46, flexShrink: 0 }} />}
          {i > 0 && (
            <select
              className="conn"
              value={cond.conn ?? 'and'}
              onChange={(e) => change(i, { conn: e.target.value as 'and' | 'or' })}
              aria-label="Связка с предыдущим условием"
            >
              {dict.connectors.map((c) => (
                <option key={c.key} value={c.key}>
                  {c.name}
                </option>
              ))}
            </select>
          )}
          <select
            className="sel"
            style={{ flexGrow: 1, minWidth: 0 }}
            value={cond.metric}
            onChange={(e) => change(i, { metric: e.target.value })}
          >
            {dict.metrics.map((m) => (
              <option key={m.key} value={m.key}>
                {m.name}
              </option>
            ))}
            {/* Недоступные показатели видны затенёнными с причиной, а не
                вырезаны молча (Архитектура ч.2 §3.6). */}
            {dict.unavailable_metrics.map((m) => (
              <option key={m.key} value={m.key} disabled title={m.reason}>
                {m.name} — недоступно
              </option>
            ))}
          </select>
          <select
            className="sel"
            style={{ width: 122, flexShrink: 0 }}
            value={cond.cmp}
            onChange={(e) => change(i, { cmp: e.target.value })}
            aria-label="Сравнение"
          >
            {dict.comparators.map((c) => (
              <option key={c.key} value={c.key}>
                {c.name}
              </option>
            ))}
          </select>
          <input
            className="num"
            style={{ width: 52, flexShrink: 0 }}
            value={String(cond.value)}
            onChange={(e) =>
              change(i, { value: e.target.value.replace(/[^\d.,]/g, '') })
            }
            aria-label="Значение условия"
          />
          <span
            className="mono"
            style={{ fontSize: 12, color: 'var(--faint)', width: 46, flexShrink: 0 }}
          >
            {unit(cond.metric)}
          </span>
          {many ? (
            <button
              className="del"
              aria-label="Убрать условие"
              onClick={() =>
                patch({
                  conditions: withoutConnectorOnFirst(
                    draft.conditions.filter((_, k) => k !== i),
                  ),
                })
              }
            >
              ×
            </button>
          ) : (
            <span style={{ width: 26, flexShrink: 0 }} />
          )}
        </div>
      ))}

      <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
        <button
          disabled={full}
          onClick={() => {
            const next = pick(dict.metrics, 'drawdown_pct')
            if (!next) return
            patch({
              conditions: [
                ...draft.conditions,
                { ...defaultCondition(next), conn: 'and' },
              ],
            })
          }}
          style={{
            padding: '7px 12px',
            borderRadius: 7,
            border: '1px dashed #3c3c36',
            fontSize: 13,
          }}
        >
          + Условие
        </button>
        <span style={{ fontSize: 12, color: '#6e6c65' }}>
          {dict.metrics.length}{' '}
          {plural(dict.metrics.length, 'показатель', 'показателя', 'показателей')},
          комбинируются через «и» / «или». Не больше {dict.max_conditions} условий.
        </span>
      </div>
    </>
  )
}

// --- мелкие части ---

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: 'flex', gap: 14, alignItems: 'flex-start' }}>
      <div style={{ width: 74, flexShrink: 0, paddingTop: 8 }}>
        <span className="klabel">{label}</span>
      </div>
      <div
        style={{
          flexGrow: 1,
          minWidth: 0,
          borderLeft: '2px solid var(--line-2)',
          paddingLeft: 16,
          display: 'flex',
          flexDirection: 'column',
          gap: 10,
        }}
      >
        {children}
      </div>
    </div>
  )
}

function Note({
  children,
  tone,
}: {
  children: React.ReactNode
  tone?: 'warn'
}) {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'flex-start',
        gap: 9,
        padding: '11px 13px',
        borderRadius: 8,
        background: tone === 'warn' ? '#2a2418' : '#22221e',
        border: `1px solid ${tone === 'warn' ? '#4a3a20' : 'var(--line)'}`,
      }}
    >
      <svg
        width="15"
        height="15"
        viewBox="0 0 24 24"
        fill="none"
        stroke={tone === 'warn' ? 'var(--warn)' : 'var(--accent)'}
        strokeWidth="2.2"
        strokeLinecap="round"
        aria-hidden="true"
        style={{ flexShrink: 0, marginTop: 1 }}
      >
        <circle cx="12" cy="12" r="9" />
        <line x1="12" y1="7" x2="12" y2="12" />
        <line x1="12" y1="12" x2="15.5" y2="14" />
      </svg>
      <span
        style={{
          fontSize: 12,
          color: tone === 'warn' ? 'var(--warn)' : 'var(--dim)',
          lineHeight: 1.6,
        }}
      >
        {children}
      </span>
    </div>
  )
}

function Pill({
  on,
  disabled,
  onClick,
}: {
  on: boolean
  disabled?: boolean
  onClick: () => void
}) {
  return (
    <button
      className={on ? 'pill on' : 'pill'}
      aria-pressed={on}
      disabled={disabled}
      onClick={onClick}
    >
      {on ? 'вкл' : 'выкл'}
    </button>
  )
}

function Toggle({
  label,
  on,
  disabled,
  note,
  onClick,
}: {
  label: string
  on: boolean
  disabled?: boolean
  note?: string
  onClick: () => void
}) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
      <span
        style={{
          fontSize: 13,
          color: on ? 'var(--fg)' : 'var(--faint)',
          whiteSpace: 'nowrap',
        }}
      >
        {label}
      </span>
      {note && <span style={{ fontSize: 12, color: '#6e6c65' }}>{note}</span>}
      <div style={{ marginLeft: 'auto' }}>
        <Pill on={on} disabled={disabled} onClick={onClick} />
      </div>
    </div>
  )
}

// --- сборка тел запросов ---

function actionsOf(draft: Draft) {
  const minutes = draft.minutes === '' ? null : Number(draft.minutes)
  const actions: Record<string, unknown> = {
    alert: draft.alert,
    lock: { enabled: draft.lockOn, minutes },
    buddy: draft.buddy,
  }
  if (draft.remindAfter !== null) {
    actions.remind_after_minutes = draft.remindAfter === '' ? 0 : Number(draft.remindAfter)
  }
  return actions
}

function previewBody(draft: Draft) {
  return {
    system_code: draft.systemCode,
    conditions: draft.conditions.map((c, i) =>
      i === 0
        ? { metric: c.metric, cmp: c.cmp, value: numberOrZero(c.value) }
        : {
            metric: c.metric,
            cmp: c.cmp,
            value: numberOrZero(c.value),
            conn: c.conn ?? 'and',
          },
    ),
    actions: actionsOf(draft),
    unlock: draft.unlock,
  }
}

// Правка правила частичная. У системного отправляем только разрешённые поля:
// всё остальное сервер отклонит, и отправлять его значило бы просить отказ.
function patchBody(draft: Draft) {
  if (draft.systemCode === null) {
    const { system_code: _ignored, ...rest } = previewBody(draft)
    return { name: draft.name, ...rest }
  }
  const body: Record<string, unknown> = {}
  const actions: Record<string, unknown> = {}
  if (can(draft, 'actions.buddy')) actions.buddy = draft.buddy
  if (can(draft, 'actions.lock.minutes')) {
    actions.lock = { minutes: draft.minutes === '' ? null : Number(draft.minutes) }
  }
  if (can(draft, 'actions.remind_after_minutes') && draft.remindAfter !== null) {
    actions.remind_after_minutes = draft.remindAfter === '' ? 0 : Number(draft.remindAfter)
  }
  if (Object.keys(actions).length > 0) body.actions = actions
  if (can(draft, 'unlock')) body.unlock = draft.unlock
  return body
}

function numberOrZero(value: number | string): number {
  const n = Number(String(value).replace(',', '.'))
  return Number.isFinite(n) ? n : 0
}

function withoutConnectorOnFirst(items: RuleCondition[]): RuleCondition[] {
  return items.map((item, i) => {
    if (i === 0) {
      const { conn: _drop, ...rest } = item
      return rest
    }
    return { ...item, conn: item.conn ?? 'and' }
  })
}

function capitalize(text: string): string {
  return text.length === 0 ? text : text[0].toUpperCase() + text.slice(1)
}
