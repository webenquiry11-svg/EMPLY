import os
import json
os.environ.setdefault('DJANGO_SETTINGS_MODULE','horilla.settings')
import django
django.setup()
from leave.models import LeaveType
res = list(LeaveType.objects.filter(name='Short Leave').values('id','leave_unit','is_active'))
print(json.dumps(res))
