# Фронт для сервера: собирается один раз и раздаётся как статика.
#
# В разработке фронт поднимает Vite со своим сервером и горячей перезагрузкой.
# На сервере это не нужно и вредно: dev-сервер держит процесс, следит
# за файлами и отдаёт несобранный код. Поэтому здесь сборка, а раздаёт
# её Caddy — он же выдаёт сертификат.

FROM node:22-slim AS build

WORKDIR /app
COPY web/package.json ./
# npm install, а не npm ci: лок фронта в репозиторий сознательно не берётся.
RUN npm install

COPY web/ ./
# Сборка падает на ошибке типов: `npm run build` это `tsc --noEmit && vite build`.
# Так сломанный фронт не уедет на сервер молча.
RUN npm run build

FROM caddy:2-alpine
COPY --from=build /app/dist /srv
COPY deploy/Caddyfile /etc/caddy/Caddyfile
