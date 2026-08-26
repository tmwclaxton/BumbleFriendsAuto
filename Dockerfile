FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ADB_SERVER_SOCKET=tcp:127.0.0.1:5037 \
    TZ=Europe/London \
    BFF_COMBINED_SERVER=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        adb \
        ca-certificates \
        curl \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# supercronic for cron in containers
ARG SUPERCRONIC_URL=https://github.com/aptible/supercronic/releases/download/v0.2.33/supercronic-linux-amd64
RUN curl -fsSL -o /usr/local/bin/supercronic "$SUPERCRONIC_URL" \
    && chmod +x /usr/local/bin/supercronic

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src
COPY config.example.yaml ./config.example.yaml
COPY docker/entrypoint.sh /entrypoint.sh
COPY docker/crontab /app/docker/crontab
RUN chmod +x /entrypoint.sh

RUN mkdir -p /app/data

EXPOSE 8765
ENTRYPOINT ["/entrypoint.sh"]
