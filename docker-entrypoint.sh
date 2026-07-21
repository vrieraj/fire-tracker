#!/bin/bash
set -e
echo "[entrypoint] Starting gunicorn..."
exec gunicorn -c gunicorn.conf.py fire_tracker.api.app:app
