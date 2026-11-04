# Production static build + Nginx reverse proxy for HTTP and WebSocket.
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi
COPY . .
ARG VITE_API_BASE=/api
ARG VITE_API_TOKEN=
ENV VITE_API_BASE=${VITE_API_BASE} VITE_API_TOKEN=${VITE_API_TOKEN}
RUN npm run typecheck && npm run build

FROM nginx:1.27-alpine
ENV AGENTFORGE_FRAME_ANCESTORS="*"
COPY nginx.conf /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
  CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1
CMD ["nginx", "-g", "daemon off;"]