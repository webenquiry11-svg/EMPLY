import os, json
os.environ.setdefault('DJANGO_SETTINGS_MODULE','horilla.settings')
import django
django.setup()
from employee.models import Employee
from leave.models import LeaveType

emp = Employee.objects.first()
if not emp:
    print(json.dumps({'error':'no employee in DB'}))
else:
    assigned_ids = list(emp.available_leave.values_list('leave_type_id', flat=True))
    assigned = list(LeaveType.objects.filter(id__in=assigned_ids).values('id','name','leave_unit'))
    minute = list(LeaveType.objects.filter(leave_unit='minute', is_active=True).values('id','name','leave_unit'))
    union_ids = {lt['id'] for lt in assigned} | {lt['id'] for lt in minute}
    union = list(LeaveType.objects.filter(id__in=union_ids).values('id','name','leave_unit'))
    print(json.dumps({'employee_id': emp.id, 'assigned': assigned, 'minute': minute, 'union': union}, default=str))
