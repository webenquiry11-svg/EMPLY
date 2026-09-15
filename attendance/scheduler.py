import os
import subprocess
import sys
import atexit

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from base.backends import logger
from horilla.process_lock import ProcessLock

MORNING_CAPTURE_START_HOUR = 9
MORNING_CAPTURE_STOP_HOUR = 10
_biometric_worker_process = None
_scheduler_lock = None


def _zk_devices():
    from biometric.models import BiometricDevices

    return BiometricDevices.objects.filter(is_active=True, machine_type="zk")


def _set_morning_capture_state(active):
    devices = _zk_devices()
    updated = devices.update(is_live=active)
    logger.info(
        "Biometric morning capture %s for %s device(s).",
        "started" if active else "stopped",
        updated,
    )
    return updated


def _ensure_biometric_worker():
    global _biometric_worker_process
    if (
        _biometric_worker_process is not None
        and _biometric_worker_process.poll() is None
    ):
        return

    _biometric_worker_process = subprocess.Popen(
        [
            sys.executable,
            os.path.join("manage.py"),
            "run_biometric_live_capture",
            "--poll-seconds",
            "5",
        ],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    logger.info(
        "Started biometric live capture supervisor process pid=%s.",
        _biometric_worker_process.pid,
    )


def _stop_biometric_worker():
    global _biometric_worker_process
    if _biometric_worker_process is None:
        return
    if _biometric_worker_process.poll() is None:
        _biometric_worker_process.terminate()
        try:
            _biometric_worker_process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            _biometric_worker_process.kill()
    logger.info("Stopped biometric live capture supervisor process.")
    _biometric_worker_process = None


atexit.register(_stop_biometric_worker)


def _acquire_scheduler_lock():
    global _scheduler_lock
    lock_path = os.path.join(str(settings.BASE_DIR), ".attendance-scheduler.lock")
    _scheduler_lock = ProcessLock(
        "Local\\EMPLY-Attendance-Scheduler", lock_path
    )
    if not _scheduler_lock.acquire():
        _scheduler_lock = None
        return False
    atexit.register(_scheduler_lock.release)
    return True


def start_morning_biometric_capture():
    try:
        _set_morning_capture_state(True)
        _ensure_biometric_worker()
    except Exception:
        _set_morning_capture_state(False)
        logger.exception("Failed to start biometric morning capture.")


def stop_morning_biometric_capture():
    _set_morning_capture_state(False)
    _stop_biometric_worker()


def recover_morning_biometric_capture():
    current_time = timezone.localtime().time()
    if is_morning_capture_window(current_time):
        start_morning_biometric_capture()
    else:
        _set_morning_capture_state(False)


def is_morning_capture_window(current_time):
    return MORNING_CAPTURE_START_HOUR <= current_time.hour < MORNING_CAPTURE_STOP_HOUR


def create_work_record():
    from attendance.models import WorkRecords
    from employee.models import Employee

    date = timezone.localdate()
    work_records = WorkRecords.objects.filter(date=date).values_list(
        "employee_id", flat=True
    )
    employees = Employee.objects.exclude(id__in=work_records)
    records_to_create = []

    for employee in employees:
        try:
            shift_schedule = employee.get_shift_schedule()
            if shift_schedule is None:
                continue

            shift = employee.get_shift()
            record = WorkRecords(
                employee_id=employee,
                date=date,
                work_record_type="DFT",
                shift_id=shift,
                message="",
            )
            records_to_create.append(record)
        except Exception as e:
            logger.error(f"Error preparing work record for {employee}: {e}")

    if records_to_create:
        try:
            WorkRecords.objects.bulk_create(records_to_create, ignore_conflicts=True)
            print(f"Created {len(records_to_create)} work records for {date}.")
        except Exception as e:
            logger.error(f"Failed to bulk create work records: {e}")
    else:
        print(f"No new work records to create for {date}.")


def reconcile_previous_biometric_day():
    from biometric.live_capture import reconcile_zk_attendance

    try:
        reconcile_zk_attendance()
    finally:
        close_old_connections()


def _database_job(job):
    def run(*args, **kwargs):
        close_old_connections()
        try:
            return job(*args, **kwargs)
        finally:
            close_old_connections()

    return run


def register_jobs(scheduler):
    scheduler.add_job(
        _database_job(create_work_record),
        "interval",
        minutes=30,
        misfire_grace_time=3600 * 3,
    )
    scheduler.add_job(
        _database_job(create_work_record),
        "cron",
        hour=0,
        minute=30,
        misfire_grace_time=3600 * 9,
        id="create_daily_work_record",
        replace_existing=True,
    )
    scheduler.add_job(
        _database_job(reconcile_previous_biometric_day),
        "cron",
        hour=0,
        minute=30,
        misfire_grace_time=3600 * 9,
        id="reconcile_previous_biometric_day",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _database_job(start_morning_biometric_capture),
        "cron",
        hour=MORNING_CAPTURE_START_HOUR,
        minute=0,
        id="start_morning_biometric_capture",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _database_job(stop_morning_biometric_capture),
        "cron",
        hour=MORNING_CAPTURE_STOP_HOUR,
        minute=0,
        id="stop_morning_biometric_capture",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )


if (
    not settings.DEBUG
    or os.environ.get("RUN_MAIN") == "true"
) and not any(
    cmd in sys.argv
    for cmd in ["makemigrations", "migrate", "compilemessages", "flush", "shell"]
    + ["check", "test"]
) and _acquire_scheduler_lock():
    """
    Initializes and starts background tasks using APScheduler when the server is running.
    """
    scheduler = BackgroundScheduler(timezone=pytz.timezone(settings.TIME_ZONE))
    register_jobs(scheduler)
    scheduler.start()
    recover_morning_biometric_capture()
