
import os
import django
import sys
from datetime import date
from django.core.exceptions import ValidationError

# Set up Django environment
sys.path.append(os.getcwd())
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'horilla.settings')
django.setup()

from django.db import transaction
from employee.models import Employee
from leave.models import LeaveType, LeaveRequest, ShortLeaveBalance

def run_short_leave_test():
    print("--- Starting Short Leave Functional Test ---")

    # Ensure a 'Short Leave' type exists
    try:
        short_leave_type, created = LeaveType.objects.get_or_create(
            name="Short Leave",
            defaults={
                'leave_unit': 'minute',
                'payment': 'paid',
                'limit_leave': True,
                'total_days': 0, # Not applicable for minute-based
                'is_active': True,
            }
        )
        if created:
            print(f"✔ VERIFIED: Created 'Short Leave' LeaveType.")
        else:
            # Ensure it is a minute based leave
            if short_leave_type.leave_unit != 'minute':
                short_leave_type.leave_unit = 'minute'
                short_leave_type.save()
                print("✔ VERIFIED: Found existing 'Short Leave' LeaveType and configured it for minutes.")
            else:
                print("✔ VERIFIED: Found existing 'Short Leave' LeaveType.")

    except Exception as e:
        print(f"❌ ISSUE FOUND: Could not create or find 'Short Leave' LeaveType. Error: {e}")
        return

    # Get an employee to test with
    employee = Employee.objects.first()
    if not employee:
        print("❌ ISSUE FOUND: No employees found in the database to perform the test.")
        return
    print(f"✔ VERIFIED: Using employee: {employee} (ID: {employee.id})")

    # --- Test Case 1: Successful Short Leave Request ---
    print("
--- Test Case 1: Successful Short Leave Request ---")
    today = date.today()
    balance = None
    try:
        with transaction.atomic():
            # Ensure employee has a balance
            balance, created = ShortLeaveBalance.objects.get_or_create(
                employee_id=employee,
                month=today.month,
                year=today.year,
                defaults={'remaining_minutes': 120}
            )
            if created:
                 print(f"Created ShortLeaveBalance for {employee} with 120 minutes.")
            else:
                # Reset balance for test idempotency
                balance.remaining_minutes = 120
                balance.save()
                print(f"Reset ShortLeaveBalance for {employee} to 120 minutes.")


            print(f"Attempting to request a 30-minute short leave for {today}...")
            leave_request = LeaveRequest(
                employee_id=employee,
                leave_type_id=short_leave_type,
                start_date=today,
                end_date=today,
                requested_minutes=30,
                description="Test: Successful short leave request",
                status="requested"
            )
            leave_request.full_clean()  # This will trigger the clean() method validation
            leave_request.save() # This will trigger the save() method logic

            print("✔ VERIFIED: LeaveRequest created successfully in memory.")
            
            # Verify database state
            balance.refresh_from_db()
            leave_request.refresh_from_db()

            if balance.remaining_minutes == 90 and leave_request.reserved_minutes == 30:
                print(f"✔ VERIFIED: Correctly reserved 30 minutes. New balance: {balance.remaining_minutes} mins.")
            else:
                print(f"❌ ISSUE FOUND: Incorrect balance or reservation.")
                print(f"   - Expected Balance: 90, Actual: {balance.remaining_minutes}")
                print(f"   - Expected Reservation: 30, Actual: {leave_request.reserved_minutes}")

            # Clean up the created request
            leave_request.delete()
            print("✔ VERIFIED: Cleaned up successful leave request.")

    except (ValidationError, ValueError) as e:
        print(f"❌ ISSUE FOUND: Test case failed unexpectedly. Error: {e}")
    except Exception as e:
        print(f"❌ An unexpected error occurred: {e}")


    # --- Test Case 2: Insufficient Balance ---
    print("
--- Test Case 2: Insufficient Balance ---")
    try:
        with transaction.atomic():
            # Reset balance for test
            balance, _ = ShortLeaveBalance.objects.get_or_create(
                employee_id=employee,
                month=today.month,
                year=today.year,
            )
            balance.remaining_minutes = 30
            balance.save()
            print(f"Set ShortLeaveBalance for {employee} to 30 minutes for this test.")

            print(f"Attempting to request a 60-minute short leave (should fail)...")
            leave_request_fail = LeaveRequest(
                employee_id=employee,
                leave_type_id=short_leave_type,
                start_date=today,
                end_date=today,
                requested_minutes=60,
                description="Test: Failing short leave request",
                status="requested"
            )

            try:
                leave_request_fail.full_clean()
                leave_request_fail.save()
                print("❌ ISSUE FOUND: LeaveRequest was saved even with insufficient balance.")
                # Rollback manually since we expect an error
                raise Exception("Manual rollback.")
            except (ValidationError, ValueError) as e:
                print(f"✔ VERIFIED: Correctly failed to create request with insufficient balance.")
                print(f"   - Validation Error: {e}")

            # Verify balance was not changed
            balance.refresh_from_db()
            if balance.remaining_minutes == 30:
                print(f"✔ VERIFIED: Balance remains unchanged at {balance.remaining_minutes} minutes.")
            else:
                print(f"❌ ISSUE FOUND: Balance was incorrectly modified. New balance: {balance.remaining_minutes}")

    except Exception as e:
         if "Manual rollback" not in str(e):
            print(f"❌ An unexpected error occurred in Test Case 2: {e}")


    print("
--- Short Leave Functional Test Finished ---")


if __name__ == "__main__":
    run_short_leave_test()
