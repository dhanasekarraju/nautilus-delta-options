FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --system --gid 10001 app \
    && useradd \
        --system \
        --uid 10001 \
        --gid app \
        --home-dir /app \
        --shell /usr/sbin/nologin \
        app

WORKDIR /app

COPY pyproject.toml constraints-py312.txt ./
COPY src ./src

RUN python -m pip install --upgrade pip \
    && python -m pip install -c constraints-py312.txt . \
    && python -m nautilus_delta_options.runtime_check \
    && mkdir -p /data \
    && chown app:app /data

USER app

VOLUME ["/data"]

HEALTHCHECK \
    --interval=30s \
    --timeout=5s \
    --start-period=120s \
    --retries=3 \
    CMD ["python", "-c", "import os; os.kill(1, 0); assert os.access('/data', os.W_OK)"]

ENTRYPOINT ["python", "-m", "nautilus_delta_options"]

CMD ["--database", "/data/paper-ledger.sqlite", "--interval-seconds", "60"]
