"""Persistent server-side live capture for ZKTeco/eSSL devices."""

import hashlib
import logging
import threading
import time
from datetime import date, datetime, timedelta

from django.db import close_old_connections, connection, transaction
from django.utils import timezone
from zk import ZK

from attendance.methods.utils import Request
from attendance.views.clock_in_out import clock_in, clock_out

from .models import BiometricDevices, BiometricEmployees, BiometricPunchEvent

logger = logging.getLogger(__name__)


def _close_connections_if_safe():
    """Refresh worker connections without closing a caller-owned transaction."""
    if not connection.in_atomic_block:
        close_old_connections()


def _event_timestamp(value):
    if timezone.is_aware(value):
        return timezone.localtime(value)
    return timezone.make_aware(value)


def _event_key(device, attendance):
    raw = "|".join(
        [
            str(device.pk),
            str(attendance.user_id),
            _event_timestamp(attendance.timestamp).isoformat(),
            str(attendance.punch),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def process_zk_punch(device, attendance):
    """Persist one device event and send it through EMPLY's existing pipeline."""
    event_timestamp = _event_timestamp(attendance.timestamp)
    event_key = _event_key(device, attendance)
    punch = attendance.punch

    try:
        with transaction.atomic():
            event, created = BiometricPunchEvent.objects.select_for_update().get_or_create(
                event_key=event_key,
                defaults={
                    "device": device,
                    "user_id": str(attendance.user_id),
                    "timestamp": event_timestamp,
                    "punch": punch,
                },
            )
            if not created and event.processed:
                return False

            mapping = BiometricEmployees.objects.filter(
                device_id=device, user_id=str(attendance.user_id)
            ).select_related("employee_id__employee_user_id").first()
            if mapping is None:
                event.processed = True
                event.processed_at = timezone.now()
                event.error = "No employee mapping for biometric user."
                event.save(update_fields=["processed", "processed_at", "error"])
                return False

            event.employee = mapping.employee_id
            if mapping.employee_id.attendance_source != "biometric_machine":
                event.processed = True
                event.processed_at = timezone.now()
                event.error = "Employee is configured for portal attendance."
                event.save(update_fields=["employee", "processed", "processed_at", "error"])
                return False
            event.save(update_fields=["employee"])
            request = Request(
                user=mapping.employee_id.employee_user_id,
                date=event_timestamp.date(),
                time=event_timestamp.time(),
                datetime=event_timestamp,
            )
            direction = device.device_direction
            if direction == "in":
                clock_in(request)
            elif direction == "out":
                clock_out(request)
            elif punch in {0, 3, 4}:
                clock_in(request)
            else:
                clock_out(request)

            event.processed = True
            event.processed_at = timezone.now()
            event.error = ""
            event.save(update_fields=["processed", "processed_at", "error"])
        return True
    finally:
        _close_connections_if_safe()


def reconcile_zk_attendance(target_date=None):
    """Replay only the completed date's missed ZKTeco events."""
    target_date = target_date or (timezone.localdate() - timedelta(days=1))
    started_at = time.monotonic()
    totals = {
        "devices": 0,
        "machine_events": 0,
        "processed": 0,
        "already_processed": 0,
        "unmapped": 0,
        "failed": 0,
    }
    logger.info("Biometric reconciliation started for %s", target_date)

    close_old_connections()
    devices = list(
        BiometricDevices.objects.filter(is_active=True, machine_type="zk")
    )
    try:
        for device in devices:
            totals["devices"] += 1
            connection = None
            try:
                zk_device = ZK(
                    device.machine_ip,
                    port=device.port,
                    timeout=5,
                    password=device.zk_password,
                    force_udp=False,
                    ommit_ping=False,
                )
                connection = zk_device.connect()
                # pyzk exposes the device's read-only attendance log through this API.
                attendances = connection.get_attendance() or []
                watermark = None
                if device.last_fetch_date and device.last_fetch_time:
                    if device.last_fetch_date == target_date:
                        watermark = device.last_fetch_time
                day_attendances = [
                    attendance
                    for attendance in attendances
                    if (
                        _event_timestamp(attendance.timestamp).date() == target_date
                        and (
                            watermark is None
                            or _event_timestamp(attendance.timestamp).time()
                            > watermark
                        )
                    )
                ]
                totals["machine_events"] += len(day_attendances)
                for attendance in day_attendances:
                    event_key = _event_key(device, attendance)
                    if BiometricPunchEvent.objects.filter(
                        event_key=event_key, processed=True
                    ).exists():
                        totals["already_processed"] += 1
                        continue
                    try:
                        if process_zk_punch(device, attendance):
                            totals["processed"] += 1
                        else:
                            event = BiometricPunchEvent.objects.filter(
                                event_key=event_key
                            ).only("error").first()
                            if event and event.error.startswith("No employee mapping"):
                                totals["unmapped"] += 1
                    except Exception:
                        totals["failed"] += 1
                        logger.exception(
                            "Failed to reconcile biometric event %s for device %s",
                            event_key,
                            device.pk,
                        )
            except Exception:
                totals["failed"] += 1
                logger.exception(
                    "Failed to read biometric logs for device %s on %s",
                    device.pk,
                    target_date,
                )
            finally:
                if connection is not None:
                    try:
                        connection.disconnect()
                    except Exception:
                        logger.exception(
                            "Failed to close biometric connection for device %s",
                            device.pk,
                        )
                close_old_connections()
    finally:
        close_old_connections()

    logger.info(
        "Biometric reconciliation completed for %s in %.2fs: %s",
        target_date,
        time.monotonic() - started_at,
        totals,
    )
    return totals


class ZKLiveCaptureWorker(threading.Thread):
    """Reconnectable worker for one persistently enabled ZKTeco/eSSL device."""

    daemon = True

    def __init__(self, device_id, retry_seconds=5):
        super().__init__(name=f"biometric-live-{device_id}")
        self.device_id = device_id
        self.retry_seconds = retry_seconds
        self.stop_event = threading.Event()

    def stop(self):
        self.stop_event.set()

    def run(self):
        while not self.stop_event.is_set():
            close_old_connections()
            device = BiometricDevices.objects.filter(
                pk=self.device_id, is_live=True, machine_type="zk"
            ).first()
            if device is None:
                return

            connection = None
            try:
                zk_device = ZK(
                    device.machine_ip,
                    port=device.port,
                    timeout=5,
                    password=int(device.zk_password),
                    force_udp=False,
                    ommit_ping=False,
                )
                connection = zk_device.connect()
                while not self.stop_event.is_set():
                    close_old_connections()
                    if not BiometricDevices.objects.filter(
                        pk=self.device_id, is_live=True
                    ).exists():
                        return
                    try:
                        for attendance in connection.live_capture():
                            if self.stop_event.is_set():
                                return
                            if attendance:
                                process_zk_punch(device, attendance)
                                close_old_connections()
                    except Exception:
                        logger.exception(
                            "Biometric live capture loop failed for device %s",
                            self.device_id,
                        )
                        close_old_connections()
                        self.stop_event.wait(self.retry_seconds)
                        break
            except Exception:
                logger.exception("Biometric live capture failed for device %s", self.device_id)
                close_old_connections()
                self.stop_event.wait(self.retry_seconds)
            finally:
                if connection is not None:
                    try:
                        connection.end_live_capture = True
                        connection.disconnect()
                    except Exception:
                        logger.exception(
                            "Failed to close biometric connection for device %s",
                            self.device_id,
                        )
                close_old_connections()


class LiveCaptureSupervisor:
    """Keeps workers aligned with the database-backed is_live state."""

    def __init__(self, poll_seconds=5):
        self.poll_seconds = poll_seconds
        self.workers = {}

    def sync(self):
        enabled_ids = set(
            BiometricDevices.objects.filter(
                is_live=True, is_active=True, machine_type="zk"
            ).values_list("pk", flat=True)
        )
        for device_id in enabled_ids:
            worker = self.workers.get(device_id)
            if worker is None or not worker.is_alive():
                worker = ZKLiveCaptureWorker(device_id)
                worker.start()
                self.workers[device_id] = worker

        for device_id in set(self.workers) - enabled_ids:
            self.workers.pop(device_id).stop()

    def stop(self):
        for worker in self.workers.values():
            worker.stop()
        self.workers.clear()

    def run(self):
        try:
            while True:
                close_old_connections()
                self.sync()
                time.sleep(self.poll_seconds)
        except KeyboardInterrupt:
            self.stop()
