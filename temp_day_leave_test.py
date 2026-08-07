import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'horilla.settings')
import django
django.setup()

from leave.models import LeaveRequest, AvailableLeave
from datetime import date, timedelta
import traceback
from django.contrib.auth import get_user_model
from types import SimpleNamespace
import horilla.horilla_middlewares as horilla_middlewares

User = get_user_model()
request = SimpleNamespace(user=User.objects.filter(is_superuser=True).first(), session={'selected_company': 'all'})
horilla_middlewares._thread_locals.request = request

available = AvailableLeave.objects.filter(leave_type_id__leave_unit='day').first()
print('available', available)
if available:
    lr = LeaveRequest(
        employee_id=available.employee_id,
        leave_type_id=available.leave_type_id,
        start_date=date.today() + timedelta(days=1),
        end_date=date.today() + timedelta(days=1),
        requested_days=1,
        description='Day leave validation test',
        status='requested'
    )
    try:
        lr.full_clean()
        print('day leave full_clean passed')
    except Exception:
        traceback.print_exc()
else:
    print('no available day leave found')
