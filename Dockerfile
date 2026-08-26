FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Europe/Kyiv

RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata \
        ca-certificates \
        curl \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --- deps layer ---
FROM base AS deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- runtime ---
FROM deps AS runtime
COPY src/ ./src/
COPY config/ ./config/
COPY scripts/ ./scripts/
COPY tests/ ./tests/
# checks/ — аналітичні читачки (twin_curve.py тощо). Без них
# задокументована команда `docker compose exec -T stakan-bot python
# /app/checks/twin_curve.py` не працює взагалі: тека в образ не їхала.
COPY checks/ ./checks/

# Non-root user
RUN useradd -m -u 1000 stakan && chown -R stakan:stakan /app
USER stakan

# Healthcheck — checks that bot main loop is alive via heartbeat file
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import os, time; \
    age = time.time() - os.path.getmtime('/app/data/.heartbeat'); \
    exit(0 if age < 60 else 1)" || exit 1

CMD ["python", "-m", "src.main"]
