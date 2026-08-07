from django.core.management.base import BaseCommand
from leave.models import LeaveType
from employee.models import Employee
import json

class Command(BaseCommand):
    help = 'Print Short Leave records for verification and verify inclusion in employee leave list'

    def handle(self, *args, **options):
        qs = LeaveType.objects.filter(name='Short Leave')
        data = list(qs.values('id','name','leave_unit','is_active'))
        out = {'count': len(data), 'records': data}

        emp = Employee.objects.first()
        if emp:
            assigned_ids = list(emp.available_leave.values_list('leave_type_id', flat=True))
            assigned = list(LeaveType.objects.filter(id__in=assigned_ids).values('id','name','leave_unit'))
            minute = list(LeaveType.objects.filter(leave_unit='minute', is_active=True).values('id','name','leave_unit'))
            union_ids = {lt['id'] for lt in assigned} | {lt['id'] for lt in minute}
            union = list(LeaveType.objects.filter(id__in=union_ids).values('id','name','leave_unit'))
            out['employee_sample'] = {'employee_id': emp.id, 'assigned': assigned, 'minute': minute, 'union': union}
        else:
            out['employee_sample'] = 'no_employee_found'

        self.stdout.write(json.dumps(out, default=str))
