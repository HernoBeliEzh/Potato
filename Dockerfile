FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POTATO_DATA_DIR=/data \
    POTATO_REQUIREMENTS_FILE=/app/requirements.lock \
    POTATO_HTTP_HOST=0.0.0.0 \
    POTATO_HTTP_PORT=8080

WORKDIR /app

RUN apt-get update && \
    apt-get install --no-install-recommends -y gcc g++ golang-go rustc nodejs && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.lock ./

RUN \
    pip install --no-cache-dir -r requirements.lock && \
    groupadd --system --gid 999 potato && \
    useradd --system --uid 999 --gid potato --home-dir /data potato && \
    mkdir -p /data && \
    chown potato:potato /data

COPY pyproject.toml README.md LICENSE ./
COPY potato ./potato
RUN pip install --no-cache-dir --no-deps .

ARG POTATO_BUILD_SHA=unknown
ENV POTATO_BUILD_SHA=${POTATO_BUILD_SHA}

USER potato

CMD ["python", "-m", "potato", "run"]
