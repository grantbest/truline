# syntax=docker/dockerfile:1.7
#
# Multi-stage build for the LifeOps Console.
#
#   stage 1 (build):  node — install + vite build → /app/dist
#   stage 2 (serve):  nginx:alpine — serve dist, proxy /substrate/* to mcp-hub
#
# Build:   docker build -t lifeops-console:latest apps/lifeops-console
# Deploy:  via build-lifeops-console.yml CI workflow into k3s containerd
#          (the substrate manifest comment documents the import dance).

# ---- stage 1: build -------------------------------------------------------
FROM node:22-alpine AS build

WORKDIR /app

# Cache layer: deps only.
COPY package.json pnpm-lock.yaml ./
RUN npm install

# App source.
COPY tsconfig.json vite.config.ts tailwind.config.js postcss.config.js index.html ./
COPY src ./src

RUN npm run build

# ---- stage 2: serve -------------------------------------------------------
FROM nginx:1.27-alpine AS serve

# envsubst lives in gettext on alpine; nginx:alpine doesn't ship it by default.
RUN apk add --no-cache gettext

# Default upstream URLs target the in-cluster services. Overlays override
# via the container env so dev/stage/prod can vary them without a rebuild.
ENV MCP_HUB_URL=http://mcp-hub.platform-mcp.svc.cluster.local:8000
# Phase 8: the finance-reporting (Dash) service backing the /reports/ proxy.
# Co-located in platform-core (same namespace as the console).
ENV FINANCE_REPORTING_URL=http://finance-reporting.platform-core.svc.cluster.local:8050

# Restrict envsubst to ONLY our own ${VARS} — otherwise nginx:alpine's
# entrypoint would also clobber every $host, $remote_addr, $scheme in the
# config, which would mangle proxy_set_header values silently. Both vars
# used in nginx.conf MUST be listed here or they'll render literally.
ENV NGINX_ENVSUBST_FILTER='^(MCP_HUB_URL|FINANCE_REPORTING_URL)$'

COPY nginx.conf /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

# nginx:alpine's entrypoint already runs envsubst on /etc/nginx/templates/*
# at container start (NGINX_ENVSUBST_TEMPLATE_DIR), so ${MCP_HUB_URL} is
# materialized into /etc/nginx/conf.d/default.conf before nginx starts.
# No custom CMD needed; inherit from the base image.

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1/healthz >/dev/null || exit 1
