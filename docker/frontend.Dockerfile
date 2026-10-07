# ── AgentForge frontend (Vite dev server) image ──────────────
# Build context: ./frontend
# Built by docker compose; for production build a static bundle and
# serve it with nginx instead.

FROM node:20-alpine

WORKDIR /app

# Layer 1: install dependencies first to maximize docker cache.
COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

# Layer 2: copy the rest of the source.
COPY . .

# Vite dev server listens on 0.0.0.0:5173.
EXPOSE 5173

# `--host 0.0.0.0` makes the dev server reachable from outside the
# container; the `--` is needed so npm forwards the flags verbatim.
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]