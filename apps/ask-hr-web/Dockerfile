FROM node:24-alpine AS base
ENV PNPM_HOME="/pnpm"
ENV PATH="$PNPM_HOME:$PATH"
RUN npm config set registry https://packagefeedproxy.microsoft.io/npm/ \
    && npm install --global pnpm@10.11.0

FROM base AS deps
WORKDIR /app
COPY .npmrc package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

FROM base AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN NEXT_TELEMETRY_DISABLED=1 \
    ENTRA_TENANT_ID=00000000-0000-0000-0000-000000000000 \
    ENTRA_CLIENT_ID=00000000-0000-0000-0000-000000000000 \
    ENTRA_CLIENT_SECRET=build-placeholder \
    ENTRA_REDIRECT_URI=https://example.invalid/api/auth/callback \
    APP_BASE_URL=https://example.invalid \
    FOUNDRY_AGENT_ENDPOINT=https://example.invalid/responses \
    AZURE_STORAGE_DFS_ENDPOINT=https://example.dfs.core.windows.net/ \
    AZURE_STORAGE_FILE_SYSTEM=hr-content \
    pnpm build

FROM base AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3000
ENV HOSTNAME=0.0.0.0
RUN addgroup --system --gid 1001 nodejs \
    && adduser --system --uid 1001 nextjs
COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
USER nextjs
EXPOSE 3000
CMD ["node", "server.js"]
