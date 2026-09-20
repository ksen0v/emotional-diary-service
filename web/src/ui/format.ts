// Форматирование для показа. Здесь нет вычислений: числа приходят с сервера
// уже посчитанными, фронт их только оформляет (Архитектура ч.2 §1.4).

export function money(value: string | number): string {
  const n = Number(value)
  const sign = n > 0 ? '+' : ''
  return `${sign}$${n.toFixed(2)}`
}

export function pct(value: string | number, digits = 2): string {
  const n = Number(value)
  const sign = n > 0 ? '+' : ''
  return `${sign}${n.toFixed(digits)}%`
}

export function pnlColor(value: string | number): string {
  const n = Number(value)
  if (n > 0) return 'var(--ok)'
  if (n < 0) return 'var(--bad)'
  return 'var(--dim)'
}

export function time(iso: string | null): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleTimeString('ru-RU', {
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function dateTime(iso: string | null): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function duration(seconds: number | null): string {
  if (seconds === null) return '—'
  if (seconds < 60) return `${seconds} с`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} мин`
  return `${Math.floor(minutes / 60)} ч ${minutes % 60} мин`
}

export const MARKING: Record<string, { label: string; color: string }> = {
  clean: { label: 'по системе', color: 'var(--ok)' },
  violation: { label: 'нарушение', color: 'var(--bad)' },
  unreviewed: { label: 'без разметки', color: 'var(--warn)' },
}
