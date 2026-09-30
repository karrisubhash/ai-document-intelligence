# The production edge: nginx serving the built frontend and proxying the API.
#
# Multi-stage, so the image carries the compiled static files and nothing of
# the toolchain that produced them - no node, no node_modules, no source.
# Build from the repository root:
#
#   docker build -f infrastructure/nginx/Dockerfile -t adi-web .

FROM node:22-alpine AS build
WORKDIR /web

# Lockfile first, so the dependency layer is reused until it changes.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
# Typecheck and unit tests gate the build: an image that fails either is
# not an artifact anyone should be able to deploy.
RUN npm run typecheck && npm test && npx vite build

FROM nginx:1.27-alpine

# Built assets are content-hashed, so they can be cached for a year; the
# templates below decide which paths get which cache policy.
COPY --from=build /web/dist /usr/share/nginx/html

# The config is a template (NGINX_SERVER_NAME is substituted at start) and
# is still bind-mounted by docker-compose.prod.yml, so an operator can
# adjust it without rebuilding. Baked in too, so the image runs on its own.
COPY infrastructure/nginx/nginx.conf /etc/nginx/templates/default.conf.template
COPY infrastructure/nginx/proxy_params /etc/nginx/conf.d/proxy_params
COPY infrastructure/nginx/security_headers /etc/nginx/conf.d/security_headers

# Periodic reload for renewed certificates, as an entrypoint script so the
# image's own templating still runs (see the script for why that matters).
COPY infrastructure/nginx/40-reload-certificates.sh /docker-entrypoint.d/40-reload-certificates.sh
RUN chmod 0755 /docker-entrypoint.d/40-reload-certificates.sh
