// Клиент API. Две обязанности: подставить CSRF-заголовок на изменяющие запросы
// и превратить тело ошибки сервера в исключение с готовым текстом.
// Текст ошибки сервер уже дал по-русски — фронт его не сочиняет (Архитектура ч.2 §1.3).

export type ApiErrorBody = {
  error: { code: string; message: string; details?: Record<string, unknown> }
}

export class ApiError extends Error {
  code: string
  status: number
  details: Record<string, unknown>

  constructor(status: number, body: ApiErrorBody | null, fallback: string) {
    super(body?.error?.message ?? fallback)
    this.code = body?.error?.code ?? 'unknown'
    this.status = status
    this.details = body?.error?.details ?? {}
  }
}

function csrfToken(): string {
  const match = document.cookie.match(/(?:^|;\s*)eds_csrf=([^;]+)/)
  return match ? decodeURIComponent(match[1]) : ''
}

const UNSAFE = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])

export async function request<T>(
  path: string,
  options: { method?: string; body?: unknown } = {},
): Promise<T> {
  const method = options.method ?? 'GET'
  const headers: Record<string, string> = {}
  if (options.body !== undefined) headers['Content-Type'] = 'application/json'
  if (UNSAFE.has(method)) headers['X-CSRF-Token'] = csrfToken()

  const res = await fetch(`/api/v1${path}`, {
    method,
    headers,
    credentials: 'same-origin',
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  })

  if (res.status === 204) return undefined as T

  const text = await res.text()
  const parsed = text ? JSON.parse(text) : null

  if (!res.ok) {
    throw new ApiError(res.status, parsed as ApiErrorBody | null, 'Запрос не прошёл.')
  }
  return parsed as T
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) => request<T>(path, { method: 'POST', body }),
  patch: <T>(path: string, body?: unknown) => request<T>(path, { method: 'PATCH', body }),
  del: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
}
