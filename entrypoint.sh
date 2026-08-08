# #!/bin/bash

# echo "Waiting for database to be ready..."
# python3 manage.py makemigrations
# python3 manage.py migrate
# python3 manage.py collectstatic --noinput
# python3 manage.py createhorillauser --first_name admin --last_name admin --username admin --password admin --email admin@example.com --phone 1234567890
# gunicorn --bind 0.0.0.0:8000 horilla.wsgi:application

#!/bin/bash
#!/bin/bash
set -e

echo "Connecting to PostgreSQL..."

python3 manage.py migrate --noinput

echo "Database migrations completed."

python3 manage.py collectstatic --noinput

echo "Starting Gunicorn..."

exec gunicorn --bind 0.0.0.0:${PORT} horilla.wsgi:application