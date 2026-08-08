#!/bin/bash
set -e

echo "Running migrations..."
python3 manage.py migrate --noinput

echo "Collecting static files..."
python3 manage.py collectstatic --noinput

echo "Creating initial admin user if not present..."
python3 manage.py createhorillauser --first_name admin --last_name admin --username admin --password admin --email admin@example.com --phone 1234567890 || true

echo "Starting Gunicorn..."
exec gunicorn --bind 0.0.0.0:${PORT:-8000} horilla.wsgi:application