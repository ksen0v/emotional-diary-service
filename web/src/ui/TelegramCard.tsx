import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { ApiError, api } from '../lib/api'
import type {
  BuddyContact,
  LinkStart,
  Me,
  NotifySettings,
  SettingsPatched,
} from '../lib/types'
import { dateTime } from './format'

// Карточка «Telegram и доверенное лицо» из прототипа `Settings.dc.html`.
// Порядок блоков и тексты — оттуда: тумблер уведомлений, бот, доверенное лицо,
// шаблон сигнала, счётчик сигналов за месяц, предупреждение про сутки.
//
// Два отступления от прототипа, оба названы вслух в README:
//
// 1. **Поле токена бота.** В прототипе бот `@ed_service_bot` захардкожен:
//    он рисовался под сервисный бот, которого у нас нет. Токен вставляет
//    Влад, поэтому поле есть, а имя бота после сохранения приходит от самого
//    Telegram — выдуманное имя в ссылке-приглашении никуда не ведёт.
// 2. **Приглашение — ссылка.** Кнопка «Пригласить» осталась на месте, но
//    по нажатию сервис выдаёт ссылку, а пересылает её трейдер сам: бот
//    Telegram не может написать первым тому, кто его не запускал.

export function useNotify() {
  return useQuery<NotifySettings>({
    queryKey: ['notify'],
    queryFn: () => api.get<NotifySettings>('/notify/settings'),
  })
}

export function TelegramCard({ me }: { me: Me }) {
  const qc = useQueryClient()
  const notify = useNotify()
  const [token, setToken] = useState('')
  const [link, setLink] = useState<LinkStart | null>(null)
  const [error, setError] = useState('')
  const [handle, setHandle] = useState('')
  const [askUnlink, setAskUnlink] = useState(false)

  const data = notify.data
  const enabled = me.settings.telegram_enabled

  function refresh() {
    qc.invalidateQueries({ queryKey: ['notify'] })
    qc.invalidateQueries({ queryKey: ['rules'] })
    qc.invalidateQueries({ queryKey: ['today'] })
  }

  const saveToken = useMutation({
    mutationFn: () => api.put('/notify/bot', { token: token.trim() }),
    onSuccess: () => {
      setToken('')
      setError('')
      refresh()
    },
    onError: (err) => setError(text(err)),
  })

  const dropToken = useMutation({
    mutationFn: () => api.del('/notify/bot'),
    onSuccess: () => {
      setLink(null)
      refresh()
    },
    onError: (err) => setError(text(err)),
  })

  const startLink = useMutation({
    mutationFn: () => api.post<LinkStart>('/notify/telegram/link'),
    onSuccess: (data) => {
      setLink(data)
      setError('')
      refresh()
    },
    onError: (err) => setError(text(err)),
  })

  const unlink = useMutation({
    mutationFn: () => api.del('/notify/telegram'),
    onSuccess: () => {
      setAskUnlink(false)
      setLink(null)
      refresh()
    },
  })

  const toggle = useMutation({
    mutationFn: (value: boolean) =>
      api.patch<SettingsPatched>('/me/settings', { telegram_enabled: value }),
    onSuccess: (patched) => {
      qc.setQueryData<Me | null>(['me'], (old) =>
        old ? { ...old, settings: patched.settings } : old,
      )
      refresh()
    },
  })

  const invite = useMutation({
    mutationFn: () =>
      api.post<{ contact: BuddyContact }>('/notify/contacts', {
        handle: handle.trim(),
      }),
    onSuccess: () => {
      setHandle('')
      setError('')
      refresh()
    },
    onError: (err) => setError(text(err)),
  })

  const reinvite = useMutation({
    mutationFn: (id: string) =>
      api.post<{ contact: BuddyContact }>(`/notify/contacts/${id}/invite`),
    onSuccess: refresh,
    onError: (err) => setError(text(err)),
  })

  const removeContact = useMutation({
    mutationFn: (id: string) => api.del(`/notify/contacts/${id}`),
    onSuccess: () => {
      setError('')
      refresh()
    },
    onError: (err) => setError(text(err)),
  })

  const cancelRemoval = useMutation({
    mutationFn: (id: string) => api.del(`/notify/contacts/${id}/removal`),
    onSuccess: refresh,
  })

  const saveTemplate = useMutation({
    mutationFn: (args: { id: string; body: string }) =>
      api.put(`/notify/contacts/${args.id}/template`, { body: args.body }),
    onSuccess: refresh,
    onError: (err) => setError(text(err)),
  })

  const bot = data?.bot
  const linked = data?.telegram.state === 'linked'
  const contact = data?.contact ?? null

  return (
    <div className="card" style={{ padding: '18px 20px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <span className="klabel">Telegram и доверенное лицо</span>
        <span
          className="chip"
          style={{ marginLeft: 'auto', color: statusColor(enabled, bot?.installed, linked) }}
        >
          {statusText(enabled, bot?.installed, linked)}
        </span>
      </div>

      {/* Один тумблер, как в прототипе. Экран блокировки каналом не считается:
          он не отправляется, а показывается, и выключить его нечем. */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          marginTop: 14,
          paddingBottom: 14,
          borderBottom: '1px solid #232320',
        }}
      >
        <span style={{ fontSize: 13, color: 'var(--dim)', flexGrow: 1 }}>
          Уведомления в Telegram
        </span>
        <button
          type="button"
          aria-pressed={enabled}
          onClick={() => toggle.mutate(!enabled)}
          className={enabled ? 'primary' : ''}
          style={{ fontSize: 12, padding: '6px 14px' }}
        >
          {enabled ? 'вкл' : 'выкл'}
        </button>
      </div>

      {!enabled && (
        <div className="hint" style={{ marginTop: 12, color: 'var(--warn)' }}>
          Останутся только алерты в интерфейсе. Если браузер закрыт, ты их не
          увидишь, и доверенному лицу сигнал не уйдёт.
        </div>
      )}

      <div style={{ marginTop: 14, opacity: enabled ? 1 : 0.4 }}>
        {/* --- бот --- */}
        {bot?.installed ? (
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <span style={{ fontSize: 13, color: 'var(--dim)', width: 120 }}>Бот</span>
            <span style={{ fontSize: 13 }}>@{bot.username}</span>
            <span className="mono hint">
              токен вставлен {bot.updated_at ? dateTime(bot.updated_at) : ''}
            </span>
            <button
              style={{ marginLeft: 'auto', fontSize: 12, padding: '6px 11px' }}
              onClick={() => dropToken.mutate()}
            >
              Убрать токен
            </button>
          </div>
        ) : (
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <label
                htmlFor="bot-token"
                style={{ fontSize: 13, color: 'var(--dim)', width: 120 }}
              >
                Токен бота
              </label>
              <input
                id="bot-token"
                className="mono"
                type="password"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                placeholder="Токен из @BotFather"
                style={{ flexGrow: 1, fontSize: 13 }}
              />
              <button
                className="primary"
                disabled={token.trim().length < 10 || saveToken.isPending}
                onClick={() => saveToken.mutate()}
                style={{ fontSize: 13 }}
              >
                {saveToken.isPending ? 'Проверяем…' : 'Проверить'}
              </button>
            </div>
            <div className="hint" style={{ marginTop: 10, marginLeft: 130 }}>
              Создай бота в @BotFather и вставь его токен. Он хранится
              зашифрованным и обратно в интерфейс не возвращается. В репозиторий
              токен не попадает.
            </div>
          </div>
        )}

        {/* --- привязка --- */}
        {bot?.installed && (
          <div style={{ marginTop: 16 }}>
            {linked ? (
              <div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  <span style={{ fontSize: 13, color: 'var(--dim)', width: 120 }}>
                    Аккаунт
                  </span>
                  <span style={{ fontSize: 13 }}>привязан</span>
                  <span className="mono hint">
                    {data?.telegram.linked_at ? dateTime(data.telegram.linked_at) : ''}
                  </span>
                  {!askUnlink && (
                    <button
                      style={{ marginLeft: 'auto', fontSize: 12, padding: '6px 11px' }}
                      onClick={() => setAskUnlink(true)}
                    >
                      Отвязать
                    </button>
                  )}
                </div>
                {askUnlink && (
                  <Warn tone="bad">
                    <span style={{ flexGrow: 1 }}>
                      После отвязки алерты и сигналы другу в Telegram
                      отправляться не будут.
                    </span>
                    <button
                      onClick={() => unlink.mutate()}
                      style={{ fontSize: 12, padding: '6px 11px', color: 'var(--bad)' }}
                    >
                      Отвязать
                    </button>
                    <button
                      onClick={() => setAskUnlink(false)}
                      style={{ fontSize: 12, padding: '6px 11px', border: 'none' }}
                    >
                      Отмена
                    </button>
                  </Warn>
                )}
              </div>
            ) : (
              <div
                style={{
                  padding: '13px 15px',
                  borderRadius: 8,
                  background: '#22221e',
                  border: '1px solid var(--line-2)',
                }}
              >
                {link ? (
                  <>
                    <div style={{ fontSize: 13 }}>
                      Отправь этот код боту @{link.bot_username}
                    </div>
                    <div
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: 12,
                        marginTop: 10,
                      }}
                    >
                      <span className="mono" style={{ fontSize: 20, letterSpacing: 2 }}>
                        {link.code}
                      </span>
                      <div style={{ flexGrow: 1 }} />
                      <a href={link.bot_url} target="_blank" rel="noreferrer">
                        Открыть бота
                      </a>
                    </div>
                    <div className="hint" style={{ marginTop: 8 }}>
                      Код действует 15 минут. Как только бот его получит, эта
                      карточка обновится сама.
                    </div>
                  </>
                ) : (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                    <span style={{ fontSize: 13, color: 'var(--dim)', flexGrow: 1 }}>
                      Аккаунт не привязан: алерты идут только в журнал сервиса.
                    </span>
                    <button
                      className="primary"
                      onClick={() => startLink.mutate()}
                      style={{ fontSize: 13 }}
                    >
                      Привязать Telegram
                    </button>
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {/* --- доверенное лицо --- */}
        <div style={{ marginTop: 18 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <span style={{ fontSize: 13, color: 'var(--dim)', width: 120 }}>
              Доверенное лицо
            </span>
            {contact ? (
              <>
                <span style={{ fontSize: 13 }}>
                  {contact.display_name ?? contact.handle}
                </span>
                <span
                  className="chip"
                  style={{
                    color: contact.awaiting_consent ? 'var(--warn)' : 'var(--ok)',
                  }}
                >
                  {contact.awaiting_consent ? 'ожидает согласия' : 'согласие получено'}
                </span>
                <div style={{ flexGrow: 1 }} />
                {contact.awaiting_consent && (
                  <button
                    style={{ fontSize: 12, padding: '6px 11px' }}
                    onClick={() => reinvite.mutate(contact.id)}
                  >
                    Новая ссылка
                  </button>
                )}
                {!contact.removal_effective_at && (
                  <button
                    style={{ fontSize: 12, padding: '6px 11px' }}
                    onClick={() => removeContact.mutate(contact.id)}
                  >
                    Удалить
                  </button>
                )}
              </>
            ) : (
              <>
                <input
                  className="mono"
                  value={handle}
                  onChange={(e) => setHandle(e.target.value)}
                  placeholder="@username"
                  aria-label="Телеграм доверенного лица"
                  style={{ flexGrow: 1, fontSize: 13 }}
                />
                <button
                  disabled={handle.trim().length < 3 || !bot?.installed}
                  onClick={() => invite.mutate()}
                  style={{ fontSize: 13 }}
                >
                  Пригласить
                </button>
              </>
            )}
          </div>

          {contact?.invite_url && contact.awaiting_consent && (
            <div
              style={{
                marginTop: 10,
                padding: '11px 13px',
                borderRadius: 8,
                background: '#22221e',
                border: '1px solid var(--line-2)',
              }}
            >
              <div className="hint">
                Перешли эту ссылку — бот не может написать первым тому, кто его
                не запускал. Друг откроет её и подтвердит согласие сам.
              </div>
              <div
                className="mono"
                style={{
                  marginTop: 8,
                  fontSize: 12,
                  color: 'var(--fg)',
                  wordBreak: 'break-all',
                }}
              >
                {contact.invite_url}
              </div>
            </div>
          )}

          {contact?.removal_effective_at && (
            <Warn tone="warn">
              <span style={{ flexGrow: 1 }}>
                Отключение вступит в силу {dateTime(contact.removal_effective_at)}. До
                этого времени сигналы продолжают уходить.
              </span>
              <button
                onClick={() => cancelRemoval.mutate(contact.id)}
                style={{ fontSize: 12, padding: '6px 11px', color: 'var(--warn)' }}
              >
                Отменить
              </button>
            </Warn>
          )}

          {contact && (
            <ContactTemplate
              contact={contact}
              onSave={(body) => saveTemplate.mutate({ id: contact.id, body })}
              saving={saveTemplate.isPending}
            />
          )}

          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 10,
              marginTop: 14,
              padding: '11px 14px',
              borderRadius: 8,
              background: '#22221e',
              border: '1px solid var(--line-2)',
            }}
          >
            <span style={{ fontSize: 13, color: 'var(--dim)' }}>
              Отправлено сигналов в этом месяце
            </span>
            <span className="mono" style={{ marginLeft: 'auto', fontSize: 17 }}>
              {data?.signals_this_month ?? 0}
            </span>
          </div>

          <div className="hint" style={{ marginTop: 14, color: 'var(--warn)' }}>
            {data?.notes.removal}
          </div>
          <div className="hint" style={{ marginTop: 10 }}>
            {data?.notes.privacy}
          </div>
        </div>
      </div>

      {error && (
        <div className="err" role="alert" style={{ marginTop: 12 }}>
          {error}
        </div>
      )}
      <div className="hint" style={{ marginTop: 12 }}>
        {data?.notes.state}
      </div>
    </div>
  )
}

function ContactTemplate({
  contact,
  onSave,
  saving,
}: {
  contact: BuddyContact
  onSave: (body: string) => void
  saving: boolean
}) {
  const [body, setBody] = useState(contact.template ?? '')
  const name = contact.display_name ?? contact.handle
  return (
    <div style={{ marginTop: 16 }}>
      {/* Имя в подписи не склоняем: «для Максим» читается как опечатка,
          а склонять имена, введённые руками, нечем. */}
      <label htmlFor="buddy-template" style={{ fontSize: 13, color: 'var(--dim)' }}>
        Шаблон сигнала · {name}
      </label>
      <textarea
        id="buddy-template"
        rows={3}
        value={body}
        onChange={(e) => setBody(e.target.value)}
        placeholder="Оставь пустым — уйдёт общий текст из раздела «Тексты уведомлений»"
        style={{ width: '100%', marginTop: 8, resize: 'none' }}
      />
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 8 }}>
        <button
          onClick={() => onSave(body)}
          disabled={saving}
          style={{ fontSize: 12, padding: '6px 12px' }}
        >
          Сохранить текст
        </button>
        <span className="hint">
          Перебивает общий шаблон сигнала: двум разным людям пишут по-разному.
          Подстановки — {'{trader_name}'}, {'{rule_name}'}, {'{day}'}. Сумм в нём
          нет.
        </span>
      </div>
    </div>
  )
}

function Warn({ tone, children }: { tone: 'warn' | 'bad'; children: React.ReactNode }) {
  const bad = tone === 'bad'
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        marginTop: 10,
        padding: '11px 13px',
        borderRadius: 8,
        background: bad ? '#2a1d1a' : '#2a2418',
        border: `1px solid ${bad ? '#4a2a24' : '#4a3a20'}`,
        fontSize: 12,
        color: bad ? '#e6c0b6' : 'var(--warn)',
        lineHeight: 1.5,
      }}
    >
      {children}
    </div>
  )
}

function statusText(enabled: boolean, installed?: boolean, linked?: boolean): string {
  if (!enabled) return 'выключен'
  if (!installed) return 'нет токена'
  return linked ? 'бот привязан' : 'бот не привязан'
}

function statusColor(enabled: boolean, installed?: boolean, linked?: boolean): string {
  if (!enabled) return 'var(--dim)'
  if (installed && linked) return 'var(--ok)'
  return 'var(--warn)'
}

function text(err: unknown): string {
  return err instanceof ApiError ? err.message : 'Не получилось.'
}
