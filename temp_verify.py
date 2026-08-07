
import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "horilla.settings")
django.setup()

from employee.models import Employee
from leave.models import LeaveRequest, LeaveType, ShortLeaveBalance
from django.contrib.auth.models import User
from base.request_and_approve import LeaveApproval

def run_verification():
    print("==============================")
    print("SHORT LEAVE VERIFICATION")
    print("==============================")

    # Get a user and employee
    user = User.objects.first()
    if not user:
        print("FAIL: No user found.")
        return
    employee = Employee.objects.filter(user=user).first()
    if not employee:
        print("FAIL: No employee found for the user.")
        return

    # Get Short Leave type
    short_leave_type = LeaveType.objects.filter(leave_unit='minute').first()
    if not short_leave_type:
        print("FAIL: Short Leave type not found.")
        return
        
    # Ensure a balance record exists and set it to a known state
    balance, created = ShortLeaveBalance.objects.get_or_create(
        employee=employee,
        leave_type=short_leave_type
    )
    
    # Reset balance for a clean test
    balance.available_minutes = 120
    balance.reserved_minutes = 0
    balance.approved_minutes = 0
    balance.save()

    balance_before = balance.available_minutes
    print(f"Balance before request: {balance_before} minutes")

    # Create a leave request
    leave_request = LeaveRequest.objects.create(
        employee=employee,
        leave_type=short_leave_type,
        requested_minutes=30,
        from_date='2026-08-06',
        to_date='2026-08-06',
        reason='Testing short leave',
        status='requested'
    )

    # Check balance immediately after request
    balance.refresh_from_db()
    balance_after_request = balance.available_minutes
    reserved_after_request = balance.reserved_minutes
    
    print(f"Balance immediately after request: {balance_after_request} minutes")
    print(f"Reserved minutes after request: {reserved_after_request} minutes")

    # Approve the request
    # NOTE: Using the actual approval logic from the system
    try:
        approval_instance = LeaveApproval(leave_request.id, employee)
        approval_instance.approve()
        leave_request.refresh_from_db()
        balance.refresh_from_db()
    except Exception as e:
        print(f"FAIL: Approval process failed with an error: {e}")
        return


    balance_after_approval = balance.available_minutes
    print(f"Balance after approval: {balance_after_approval} minutes")
    
    # Verification
    # As per user expected behaviour, available minutes should not change on request, only on approval.
    # But as per my last fix, available minutes are deducted and moved to reserved on request.
    # The user's new instruction is to check if deduction happens ONLY after approval.
    # This means available_minutes should remain unchanged after request.
    
    # The current implementation I worked on deducts from available and adds to reserved.
    # Let's test against the user's explicit new requirement.
    
    fail = False
    if balance_after_request != balance_before:
        print("
❌ FAIL: Balance was deducted immediately after the request was submitted.")
        fail = True
    elif balance_after_approval != (balance_before - 30):
        print(f"
❌ FAIL: Balance after approval is incorrect. Expected {balance_before - 30}, but got {balance_after_approval}.")
        fail = True
    
    if not fail:
        print("
✅ PASS: Deduction happens only after approval.")


if __name__ == "__main__":
    run_verification()
