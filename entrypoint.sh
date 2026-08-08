#!/bin/bash
set -e

echo "=== 1. Running Database Migrations ==="
python3 manage.py migrate --noinput

echo "=== 2. Collecting Static Files ==="
python3 manage.py collectstatic --noinput

echo "=== 3. Creating Initial Horilla Admin User ==="
python3 manage.py createhorillauser \
  --first_name admin \
  --last_name admin \
  --username admin \
  --password admin \
  --email admin@example.com \
  --phone 1234567890 || true

echo "=== 4. Starting Gunicorn Web Server ==="
exec gunicorn horilla.wsgi:application --bind 0.0.0.0:${PORT:-8000}