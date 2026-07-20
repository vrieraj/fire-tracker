"""Gunicorn configuration for Fire Tracker."""

import os

bind = '0.0.0.0:7860'
workers = 2
timeout = 120
keepalive = 5
accesslog = '-'
errorlog = '-'
loglevel = os.environ.get('LOG_LEVEL', 'info')
