FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libproj-dev \
    cron \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install --no-cache-dir -e .

COPY gunicorn.conf.py docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

ENV MPLCONFIGDIR=/tmp
ENV DB_PATH=/data/fires.db
ENV GRADIO_SERVER_NAME=0.0.0.0

EXPOSE 7860

ENTRYPOINT ["/app/docker-entrypoint.sh"]
