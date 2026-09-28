#!/bin/bash
set -e

echo "=== Starting Gunicorn directly ==="
exec gunicorn horilla.wsgi:application --bind 0.0.0.0:${PORT:-8000}