# One Python image for every backend service; docker-compose.yml picks the command per service:
#   api        uvicorn src.main:app                                         :8000
#   upstreams  uvicorn --factory src.upstreams.mock_app:create_mock_upstream_app  :9000
#   mcp        python -m mcp_server.server --transport http                  :8001
#   chat       uvicorn chat_backend.app:app                                  :8100
#
# Targets: `runtime` (default, last stage) and `debug` (adds debugpy; used by docker-compose.debug.yml).

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd --create-home appuser && mkdir -p data logs && chown -R appuser /app
# Each service sets HEALTHCHECK_URL to its own health endpoint (default: the platform API).
ENV HEALTHCHECK_URL=http://localhost:8000/healthz
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(os.environ['HEALTHCHECK_URL'], timeout=2)"
USER appuser

FROM base AS debug
USER root
RUN pip install --no-cache-dir "debugpy>=1.8"
USER appuser
EXPOSE 5678
CMD ["python", "-m", "debugpy", "--listen", "0.0.0.0:5678", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS runtime
EXPOSE 8000
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
