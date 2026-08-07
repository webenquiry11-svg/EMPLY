from leave.models import LeaveType, LeaveRequest
from employee.models import Employee
from datetime import date, timedelta
import traceback

day_leave = LeaveType.objects.filter(leave_unit='day').first()
emp = Employee.objects.filter(employee_user_id__isnull=False).first()
ld2 = date.today() + timedelta(days=2)
lr2 = LeaveRequest(
    employee_id=emp,
    leave_type_id=day_leave,
    start_date=ld2,
    end_date=ld2,
    requested_days=1,
    description='Day leave auto-test',
    status='requested'
)
try:
    lr2.full_clean()
    print('day leave full_clean passed')
except Exception:
    traceback.print_exc()
