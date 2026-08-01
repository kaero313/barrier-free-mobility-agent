FROM node:22-alpine AS dependencies

ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

FROM dependencies AS builder

COPY . .
RUN npm run build

FROM node:22-alpine AS production-dependencies

ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund \
    && npm cache clean --force

FROM node:22-alpine AS runtime

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000

RUN addgroup --system --gid 10001 web \
    && adduser --system --uid 10001 --ingroup web web

WORKDIR /app

COPY --from=production-dependencies --chown=web:web /app/node_modules ./node_modules
COPY --from=builder --chown=web:web /app/.next ./.next
COPY --from=builder --chown=web:web /app/public ./public
COPY --from=builder --chown=web:web /app/package.json ./package.json

USER web

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]

CMD ["npm", "run", "start"]
