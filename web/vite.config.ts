import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Фронт и бэк живут на одном origin: /api проксируется на процесс api.
// Поэтому куки-сессия работает без CORS — это и было одним из аргументов
// за куки вместо токена (Архитектура ч.2 §1.1).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.API_URL ?? 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
