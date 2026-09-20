import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, api } from './api'
import type { Me } from './types'

// Кто вошёл. 401 — это не ошибка загрузки, а ответ «никто»:
// поэтому запрос не повторяется и не показывает красный текст.
export function useMe() {
  return useQuery<Me | null>({
    queryKey: ['me'],
    retry: false,
    queryFn: async () => {
      try {
        return await api.get<Me>('/me')
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) return null
        throw err
      }
    },
  })
}

export function useSetMe() {
  const qc = useQueryClient()
  return (me: Me | null) => qc.setQueryData(['me'], me)
}
