# Customer Support Platform UI: build with Vite, serve the static files with nginx.
# VITE_CHAT_API_URL is baked in at build time and is the URL the *browser* uses to reach the chat backend.
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
ARG VITE_CHAT_API_URL=http://localhost:8100
ARG VITE_PLATFORM_API_URL=http://localhost:8000
ARG VITE_RECEIVER_URL=http://localhost:8200
ENV VITE_CHAT_API_URL=$VITE_CHAT_API_URL VITE_PLATFORM_API_URL=$VITE_PLATFORM_API_URL VITE_RECEIVER_URL=$VITE_RECEIVER_URL
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
HEALTHCHECK --interval=10s --timeout=3s --retries=5 CMD wget -qO- http://127.0.0.1/ >/dev/null || exit 1
