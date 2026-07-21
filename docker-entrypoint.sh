#!/bin/bash
set -e

DB_PATH="${DB_PATH:-/data/fires.db}"
mkdir -p "$(dirname "$DB_PATH")"

# Run initial orchestrator (non-blocking on failure)
cd /app
python -m fire_tracker.orchestrator || echo "[entrypoint] orchestrator initial run failed, continuing..."

# Setup cron: refresh every 30 minutes
echo "*/30 * * * * cd /app && DB_PATH=$DB_PATH python -m fire_tracker.orchestrator >> /var/log/cron.log 2>&1" > /etc/cron.d/fire-tracker-cron
chmod 0644 /etc/cron.d/fire-tracker-cron
crontab /etc/cron.d/fire-tracker-cron
service cron start 2>/dev/null || cron

echo "[entrypoint] Starting gunicorn (Gradio app) on :7860..."
exec gunicorn -c gunicorn.conf.py fire_tracker.gradio_app:app
