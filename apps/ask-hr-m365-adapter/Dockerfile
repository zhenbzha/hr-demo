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
COPY package.json tsconfig.json eslint.config.js ./
COPY src ./src
RUN pnpm build

FROM node:24-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV PORT=3978
RUN addgroup --system --gid 1001 nodejs \
    && adduser --system --uid 1001 adapter
COPY --from=builder --chown=adapter:nodejs /app/dist ./dist
COPY --from=builder --chown=adapter:nodejs /app/package.json ./
COPY --from=deps --chown=adapter:nodejs /app/node_modules ./node_modules
USER adapter
EXPOSE 3978
CMD ["node", "dist/src/index.js"]
