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

echo "Checking Neon PostgreSQL connection..."

python3 manage.py shell -c "
from django.db import connection
connection.ensure_connection()
print('========================================')
print('DATABASE CONNECTED SUCCESSFULLY')
print('Database:', connection.settings_dict.get('NAME'))
print('Host:', connection.settings_dict.get('HOST'))
print('Engine:', connection.vendor)
print('========================================')
"

echo "Collecting static files..."
python3 manage.py collectstatic --noinput

echo "Starting Gunicorn on port ${PORT}..."
exec gunicorn --bind 0.0.0.0:${PORT} horilla.wsgi:application