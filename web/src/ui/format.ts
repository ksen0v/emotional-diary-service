// Форматирование для показа. Здесь нет вычислений: числа приходят с сервера
// уже посчитанными, фронт их только оформляет (Архитектура ч.2 §1.4).

// Знак идёт перед долларом, а не перед числом: «$-2520.00» читается как
// сломанная вёрстка, а не как убыток. Минус — типографский, он не путается
// с дефисом в мелком моноширинном тексте.
export function money(value: string | number): string {
  const n = Number(value)
  return `${sign(n)}$${Math.abs(n).toFixed(2)}`
}

// Короткая форма для клеток календаря: без центов и с разделителем тысяч.
// В клетку шириной в палец «+$2093.20» не помещается и вылезает за рамку,
// а точность до цента там и не нужна — она нужна в карточке дня.
export function moneyShort(value: string | number): string {
  const n = Number(value)
  return `${sign(n)}$${Math.round(Math.abs(n)).toLocaleString('ru-RU')}`
}

export function pct(value: string | number, digits = 2): string {
  const n = Number(value)
  return `${sign(n)}${Math.abs(n).toFixed(digits)}%`
}

// Просадка без знака: она по определению неотрицательна, и «+1.40%»
// читается как прибыль.
export function pctPlain(value: string | number, digits = 2): string {
  return `${Math.abs(Number(value)).toFixed(digits)}%`
}

function sign(n: number): string {
  if (n > 0) return '+'
  if (n < 0) return '\u2212'
  return ''
}

// Русские окончания: «1 торговый день», «3 торговых дня», «5 торговых дней».
export function plural(n: number, one: string, few: string, many: string): string {
  const rest = Math.abs(n) % 100
  if (rest > 10 && rest < 20) return many
  const last = rest % 10
  if (last === 1) return one
  if (last > 1 && last < 5) return few
  return many
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
