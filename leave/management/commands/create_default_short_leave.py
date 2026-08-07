from django.core.management.base import BaseCommand
from django.db import transaction
from types import SimpleNamespace

from leave.models import LeaveType
from horilla import horilla_middlewares


class Command(BaseCommand):
    help = "Create a default Short Leave LeaveType with leave_unit='minute' if none exists. Idempotent."

    def handle(self, *args, **options):
        # Ensure migrations have been applied that add the leave_unit field
        try:
            # If any LeaveType already has leave_unit='minute', do nothing
            existing = LeaveType.objects.filter(leave_unit="minute").first()
        except Exception as e:
            self.stdout.write(self.style.WARNING(
                f"Could not query LeaveType (migrations may not be applied yet): {e}. Exiting without changes."))
            return

        if existing:
            self.stdout.write(self.style.SUCCESS(
                f"Short Leave-type already exists: {existing}. No action taken."))
            return

        # Set a temporary dummy request in thread-locals so model.save() code that
        # expects request.session won't raise during this management command.
        prev_request = getattr(horilla_middlewares._thread_locals, "request", None)
        dummy_request = SimpleNamespace()
        dummy_request.session = {}
        dummy_request.user = SimpleNamespace(is_authenticated=False, is_anonymous=True)
        horilla_middlewares._thread_locals.request = dummy_request

        try:
            with transaction.atomic():
                # Create a default Short Leave record. Use conservative defaults so existing flows are unaffected.
                leave_type, created = LeaveType.objects.get_or_create(
                    name="Short Leave",
                    defaults={
                        "leave_unit": "minute",
                        "is_active": True,
                        # Minimal safe defaults; other fields rely on model defaults
                    },
                )

                # If a LeaveType named 'Short Leave' existed but had leave_unit!='minute', update leave_unit only if no other minute-type exists
                if not created and leave_type.leave_unit != "minute":
                    # Re-check to avoid overwriting if some other minute leave was created in parallel
                    if not LeaveType.objects.filter(leave_unit="minute").exclude(id=leave_type.id).exists():
                        leave_type.leave_unit = "minute"
                        leave_type.is_active = True
                        leave_type.save()
                        self.stdout.write(self.style.SUCCESS(
                            f"Updated existing LeaveType (id={leave_type.id}) name='Short Leave' to leave_unit='minute'."))
                    else:
                        self.stdout.write(self.style.WARNING(
                            "A minute-based LeaveType already exists; leaving existing 'Short Leave' record unchanged."))
                        return

        finally:
            # Restore previous request object
            horilla_middlewares._thread_locals.request = prev_request

        self.stdout.write(self.style.SUCCESS("Short Leave LeaveType ensured (leave_unit='minute')."))
