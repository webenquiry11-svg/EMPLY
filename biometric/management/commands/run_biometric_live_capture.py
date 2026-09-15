import atexit
import os

from django.core.management.base import BaseCommand

from biometric.live_capture import LiveCaptureSupervisor
from horilla.process_lock import ProcessLock


class Command(BaseCommand):
    help = "Run persistent server-side biometric live capture workers."

    def add_arguments(self, parser):
        parser.add_argument("--poll-seconds", type=int, default=5)

    def handle(self, *args, **options):
        lock = ProcessLock(
            "Local\\EMPLY-Biometric-Live-Capture",
            os.path.join(os.getcwd(), ".biometric-live-capture.lock"),
        )
        if not lock.acquire():
            self.stdout.write(
                self.style.WARNING("Biometric live capture worker is already running.")
            )
            return
        atexit.register(lock.release)
        supervisor = LiveCaptureSupervisor(options["poll_seconds"])
        self.stdout.write(self.style.SUCCESS("Biometric live capture worker started."))
        try:
            supervisor.run()
        finally:
            lock.release()
