#!/bin/bash
set -e

echo "=== 1. Running Migrations ==="
python3 manage.py migrate --noinput

echo "=== 2. Collecting Static Files ==="
python3 manage.py collectstatic --noinput

echo "=== 3. Starting Gunicorn Server ==="
exec gunicorn horilla.wsgi:application --bind 0.0.0.0:${PORT:-8000}