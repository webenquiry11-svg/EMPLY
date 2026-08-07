from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from employee.models import Employee
from attendance.models import Attendance
from datetime import date, time

class Command(BaseCommand):
    help = 'Creates test data for API verification'

    def handle(self, *args, **options):
        # Find the integration user
        try:
            user = User.objects.get(username='horilla')
        except User.DoesNotExist:
            self.stdout.write(self.style.ERROR('User "horilla" not found. Please run create_integration_user first.'))
            return

        # Create an employee for the user if it doesn't exist
        employee, created = Employee.objects.get_or_create(
            employee_user_id=user,
            defaults={
                'employee_first_name': 'Test',
                'employee_last_name': 'Employee',
                'email': 'test_employee@example.com',
                'badge_id': 'TEST-001'
            }
        )

        if created:
            self.stdout.write(self.style.SUCCESS(f'Created employee "{employee.get_full_name()}"'))
        else:
            self.stdout.write(self.style.SUCCESS(f'Found employee "{employee.get_full_name()}"'))

        # Create a complete attendance record
        Attendance.objects.get_or_create(
            employee_id=employee,
            attendance_date=date(2026, 8, 6),
            defaults={
                'attendance_clock_in': time(9, 5, 17),
                'attendance_clock_out': time(18, 14, 36)
            }
        )
        self.stdout.write(self.style.SUCCESS('Created complete attendance record.'))

        # Create an attendance record with no checkout
        Attendance.objects.get_or_create(
            employee_id=employee,
            attendance_date=date(2026, 8, 7),
            defaults={
                'attendance_clock_in': time(9, 1, 0),
                'attendance_clock_out': None
            }
        )
        self.stdout.write(self.style.SUCCESS('Created attendance record with no checkout.'))
