FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POTATO_DATA_DIR=/data \
    POTATO_REQUIREMENTS_FILE=/app/requirements.lock \
    POTATO_HTTP_HOST=0.0.0.0 \
    POTATO_HTTP_PORT=8080

WORKDIR /app

COPY pyproject.toml README.md requirements.lock LICENSE ./
COPY potato ./potato

RUN pip install --no-cache-dir -r requirements.lock && \
    pip install --no-cache-dir --no-deps . && \
    groupadd --system potato && \
    useradd --system --gid potato --home-dir /data potato && \
    mkdir -p /data && \
    chown potato:potato /data

USER potato

CMD ["python", "-m", "potato", "run"]
