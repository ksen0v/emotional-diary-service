import { useQueryClient } from '@tanstack/react-query'
import {
  createContext,
  createElement,
  useContext,
  useEffect,
  useRef,
  useState,
} from 'react'

/**
 * Живое обновление экрана: одно соединение на вкладку.
 *
 * Зачем, если есть опрос. Блокировка возникает не по действию в интерфейсе,
 * а по событию извне: приехала сделка — сработало правило. При опросе раз
 * в тридцать секунд трейдер полминуты смотрит на обычный экран, хотя сервис
 * уже решил, что торговать нельзя. Для сервиса, чья работа — встать между
 * импульсом и следующей сделкой, эти полминуты обесценивают половину работы.
 *
 * **Правило, которое держит это в рамках:** событие — сигнал «перечитай»,
 * а не источник состояния. Экран собирается из `today`, `trades`, `curve`;
 * поток только сбрасывает их кэш. Если бы экран копил состояние из событий,
 * первый же пропущенный разрыв дал бы расхождение, которое невозможно
 * воспроизвести, — а разрывы здесь норма.
 *
 * Исключение одно и осознанное: числа открытой позиции. Они меняются каждую
 * секунду, и перечитывать ленту на каждый тик значило бы двадцать запросов
 * в минуту ради одной строки. Их несёт само событие, и ровно они на экране
 * помечены как живые.
 */

// Что присылает сервер (`eds/app/ui_stream.py`).
export type StreamEvent =
  | 'trade_ingested'
  | 'trade_opened'
  | 'open_trade_tick'
  | 'trade_marked'
  | 'lock_started'
  | 'lock_lifted'
  | 'lock_breached'
  | 'incident_opened'
  | 'streak_changed'
  | 'sync_state'
  | 'telegram_linked'
  | 'buddy_confirmed'

export type OpenTradeTick = {
  trade_id: string
  symbol: string
  side: 'long' | 'short'
  open_time: string
  profit_usd: string
  account_return_pct: string
  percent: string | null
  marking: string
  trading_day: string
}

export type LiveState = {
  // Поток либо есть, либо нет. Третьего состояния на экране не нужно:
  // «подключаюсь» мигало бы на каждом разрыве, которых по норме много.
  connected: boolean
  // Вкладок больше лимита — эта переходит на опрос. Не ошибка: так и задумано
  // (Архитектура ч.2 §6), поэтому и показывается спокойным текстом.
  polling: boolean
  ticks: Record<string, OpenTradeTick>
}

// Какие кэши сбрасывает какое событие. Таблицей, а не свитчем в обработчике:
// список того, что меняется от события, — это и есть контракт с экраном,
// и он должен читаться одним взглядом.
const INVALIDATES: Record<StreamEvent, string[]> = {
  trade_ingested: ['today', 'trades', 'curve', 'marking-metrics'],
  // Новая открытая строка приходит перечитыванием: плеча, размера и тегов
  // в событии нет, а строка без них соврала бы.
  trade_opened: ['trades', 'curve'],
  // А вот тик ленту не трогает — иначе на каждое движение цены уходил бы
  // запрос, и «живое обновление» превратилось бы в двадцать запросов в минуту.
  open_trade_tick: [],
  trade_marked: ['today', 'trades', 'curve', 'marking-metrics', 'incidents'],
  lock_started: ['today', 'incidents'],
  lock_lifted: ['today', 'incidents'],
  lock_breached: ['today', 'incidents'],
  incident_opened: ['today', 'incidents'],
  streak_changed: ['today', 'streak'],
  sync_state: ['today'],
  // Привязка и согласие завершаются в боте, а не в браузере: без этого
  // трейдер сидел бы перед формой с кодом и гадал, дошло ли.
  telegram_linked: ['notify', 'rules'],
  buddy_confirmed: ['notify', 'rules'],
}

// Пауза перед повторным соединением. Растёт до потолка: если сервер лежит,
// биться в него каждую секунду со всех вкладок — худшее, что можно сделать.
const RETRY_START_MS = 1000
const RETRY_MAX_MS = 30000

export function useLiveStream(enabled: boolean): LiveState {
  const qc = useQueryClient()
  const [state, setState] = useState<LiveState>({
    connected: false,
    polling: false,
    ticks: {},
  })
  // В ref, потому что обработчики живут дольше рендера: пересоздавать
  // соединение на каждое изменение состояния — это рвать его самому себе.
  const retry = useRef(RETRY_START_MS)

  useEffect(() => {
    if (!enabled) return
    let source: EventSource | null = null
    let timer: number | undefined
    let stopped = false

    function open() {
      if (stopped) return
      source = new EventSource('/api/v1/stream', { withCredentials: true })

      source.onopen = () => {
        retry.current = RETRY_START_MS
        setState((s) => ({ ...s, connected: true, polling: false }))
      }

      source.onerror = () => {
        // EventSource не различает «нет сети» и «сервер отказал». Отличить
        // отказ по лимиту можно только отдельным запросом, и он того не стоит:
        // экран и так остаётся рабочим на опросе.
        source?.close()
        setState((s) => ({ ...s, connected: false, polling: true }))
        if (stopped) return
        timer = window.setTimeout(open, retry.current)
        retry.current = Math.min(retry.current * 2, RETRY_MAX_MS)
      }

      for (const type of Object.keys(INVALIDATES) as StreamEvent[]) {
        source.addEventListener(type, (raw) => {
          const data = parse((raw as MessageEvent).data)
          if (type === 'open_trade_tick') {
            applyTick(setState, data)
            return
          }
          for (const key of INVALIDATES[type]) {
            qc.invalidateQueries({ queryKey: [key] })
          }
        })
      }
    }

    open()
    return () => {
      stopped = true
      if (timer) window.clearTimeout(timer)
      source?.close()
    }
  }, [enabled, qc])

  return state
}

function parse(raw: string): unknown {
  try {
    return JSON.parse(raw)
  } catch {
    return null
  }
}

function applyTick(
  setState: React.Dispatch<React.SetStateAction<LiveState>>,
  data: unknown,
): void {
  const tick = data as OpenTradeTick | null
  if (!tick || typeof tick.trade_id !== 'string') return
  setState((s) => ({ ...s, ticks: { ...s.ticks, [tick.trade_id]: tick } }))
}

// Одно соединение на приложение, а не на экран. Контекст, потому что живое
// состояние нужно и топ-бару, и ленте сделок: два соединения означали бы два
// места, где можно разойтись, и вдвое быстрее упёртый лимит вкладок.
const LiveContext = createContext<LiveState>({
  connected: false,
  polling: false,
  ticks: {},
})

export function LiveProvider({
  enabled,
  children,
}: {
  enabled: boolean
  children: React.ReactNode
}) {
  const live = useLiveStream(enabled)
  return createElement(LiveContext.Provider, { value: live }, children)
}

export function useLive(): LiveState {
  return useContext(LiveContext)
}
