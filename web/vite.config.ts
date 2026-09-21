import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Фронт и бэк живут на одном origin: /api проксируется на процесс api.
// Поэтому куки-сессия работает без CORS — это и было одним из аргументов
// за куки вместо токена (Архитектура ч.2 §1.1).
export default defineConfig({
  plugins: [react()],
  server: {
    // host: true — слушать не только localhost внутри контейнера,
    // иначе порт наружу не пробрасывается.
    host: true,
    port: 5173,
    // Без strictPort занятый порт молча уехал бы на 5174,
    // и снаружи получилось бы «connection refused» без объяснений.
    strictPort: true,
    // На bind-монтировании Windows события файловой системы не доходят:
    // без опроса горячая перезагрузка молчит.
    watch: { usePolling: true, interval: 300 },
    proxy: {
      '/api': {
        target: process.env.API_URL ?? 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
