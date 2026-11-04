FROM node:22-bookworm-slim AS frontend
WORKDIR /build/frontend
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS runtime
COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock ./
COPY packages/deploy-engine/ ./packages/deploy-engine/
RUN uv sync --frozen --no-dev
COPY app/ ./app/
COPY scripts/ ./scripts/
COPY --from=frontend /build/frontend/dist/ ./frontend/dist/
RUN groupadd -g 1001 ows && useradd -u 1001 -g ows -d /app ows && mkdir /app/var && chown ows:ows /app/var
USER 1001:1001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
