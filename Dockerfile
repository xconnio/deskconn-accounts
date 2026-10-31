FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt update && apt install -y git curl make && apt clean

RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

# Copy project files
COPY pyproject.toml main.py alembic.ini /app/
COPY deskconn/ /app/deskconn/
COPY alembic/ /app/alembic/
COPY docker/postgres/init/ /app/postgres-init/

# Create default venv (.venv) and install project
RUN uv venv && uv pip install . -U

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

COPY --from=builder /app /app

CMD ["python", "main.py"]
