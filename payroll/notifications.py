"""Helpers for creating payroll-scoped in-app notifications."""

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.urls import reverse

from employee.models import Employee, EmployeeWorkInformation
from notifications.signals import notify


def send_payroll_notification(actor, recipient, verb, **kwargs):
    """Create a notification marked for the payroll workspaces."""
    return notify.send(
        actor,
        recipient=recipient,
        verb=verb,
        payroll_event=True,
        **kwargs,
    )


def payroll_admin_recipients(company=None):
    """Return active users authorized to manage payroll, scoped to a company."""
    user_model = get_user_model()
    recipients = user_model.objects.filter(is_active=True).filter(
        Q(is_superuser=True)
        | Q(
            user_permissions__content_type__app_label="payroll",
            user_permissions__codename="view_contract",
        )
        | Q(
            groups__permissions__content_type__app_label="payroll",
            groups__permissions__codename="view_contract",
        )
    ).distinct()
    if company is None:
        return recipients

    company_recipients = []
    for user in recipients:
        employee = Employee.objects.filter(employee_user_id=user).first()
        work_info = EmployeeWorkInformation.objects.filter(
            employee_id=employee
        ).first() if employee is not None else None
        if (
            user.is_superuser
            or work_info is None
            or work_info.company_id_id == company.pk
        ):
            company_recipients.append(user)
    return company_recipients


def notify_payroll_admins(actor, company, verb, **kwargs):
    """Notify payroll administrators authorized for the affected company."""
    recipients = payroll_admin_recipients(company)
    if not recipients:
        return []
    return send_payroll_notification(
        actor,
        recipient=recipients,
        verb=verb,
        **kwargs,
    )


def payroll_run_notification_url(run):
    return reverse("payroll-run-detail", kwargs={"run_id": run.pk})


def notify_payslip_status(actor, payslip, previous_status):
    """Notify the employee and payroll admins when a payslip status changes."""
    employee_user = payslip.employee_id.employee_user_id
    if employee_user is None or previous_status == payslip.status:
        return

    period = f"{payslip.start_date:%d %b %Y}–{payslip.end_date:%d %b %Y}"
    send_payroll_notification(
        actor,
        recipient=employee_user,
        verb=f"Your payslip for {period} is now {payslip.get_status_display()}.",
        redirect=reverse(
            "view-created-payslip",
            kwargs={"payslip_id": payslip.pk},
        ),
        icon="document-text-outline",
    )
    work_info = EmployeeWorkInformation.objects.filter(
        employee_id=payslip.employee_id
    ).first()
    company = work_info.company_id if work_info else None
    notify_payroll_admins(
        actor,
        company,
        f"{payslip.employee_id}'s payslip for {period} is now {payslip.get_status_display()}.",
        redirect=reverse(
            "view-created-payslip",
            kwargs={"payslip_id": payslip.pk},
        ),
        icon="document-text-outline",
    )
