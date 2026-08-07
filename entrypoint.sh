# #!/bin/bash

# echo "Waiting for database to be ready..."
# python3 manage.py makemigrations
# python3 manage.py migrate
# python3 manage.py collectstatic --noinput
# python3 manage.py createhorillauser --first_name admin --last_name admin --username admin --password admin --email admin@example.com --phone 1234567890
# gunicorn --bind 0.0.0.0:8000 horilla.wsgi:application
#!/bin/sh
set -e

echo "PORT=$PORT"
echo "Waiting for database..."

python3 manage.py migrate --noinput
python3 manage.py collectstatic --noinput

python3 manage.py createhorillauser \
  --first_name admin \
  --last_name admin \
  --username admin \
  --password admin \
  --email admin@example.com \
  --phone 1234567890 || true

exec gunicorn \
  --bind 0.0.0.0:${PORT} \
  horilla.wsgi:application \
  --access-logfile - \
  --error-logfile - \
  --log-level debug