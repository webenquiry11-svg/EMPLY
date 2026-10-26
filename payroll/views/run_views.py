"""Payroll-run preview, submission, approval, and finalization views."""

import json
from datetime import date

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Max, Q
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from base.models import Company
from employee.models import Employee
from payroll.notifications import (
    notify_payroll_admins,
    payroll_run_notification_url,
    send_payroll_notification,
)
from payroll.models.models import (
    Contract,
    Payslip,
    PayrollApprovalEvent,
    PayrollRun,
    PayrollRunItem,
)

RUN_ACTION_SOURCE_STATUS = {
    "submit": "draft",
    "approve": "submitted",
    "reject": "submitted",
    "finalize": "approved",
}


class PayrollRunForm(forms.Form):
    company = forms.ModelChoiceField(queryset=Company.objects.none())
    start_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date", "class": "oh-input w-100"})
    )
    end_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date", "class": "oh-input w-100"})
    )
    employees = forms.ModelMultipleChoiceField(
        queryset=Employee.objects.none(),
        widget=forms.SelectMultiple(attrs={"class": "oh-select w-100", "size": 12}),
    )

    def __init__(self, *args, user, **kwargs):
        request = kwargs.pop("request", None)
        super().__init__(*args, **kwargs)
        self.user = user
        selected_company = (
            request.session.get("selected_company") if request is not None else None
        )
        if selected_company and selected_company not in {"all", "none"}:
            self.fields["company"].queryset = Company.objects.filter(
                pk=selected_company
            ).order_by("company")
        elif user.is_superuser or user.has_perm("base.change_company"):
            self.fields["company"].queryset = Company.objects.all().order_by("company")
        else:
            employee = getattr(user, "employee_get", None)
            company_id = getattr(
                getattr(getattr(employee, "employee_work_info", None), "company_id", None),
                "id",
                None,
            )
            self.fields["company"].queryset = Company.objects.filter(pk=company_id)

        eligible_ids = Contract.objects.filter(
            contract_status="active"
        ).values_list("employee_id", flat=True)
        employees = Employee.objects.filter(is_active=True, id__in=eligible_ids)
        if selected_company and selected_company not in {"all", "none"}:
            employees = employees.filter(
                employee_work_info__company_id=selected_company
            )
        self.fields["employees"].queryset = (
            employees
            .select_related("employee_work_info")
            .order_by("badge_id")
            .distinct()
        )
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "oh-input w-100")

    def clean(self):
        cleaned_data = super().clean()
        start_date = cleaned_data.get("start_date")
        end_date = cleaned_data.get("end_date")
        company = cleaned_data.get("company")
        employees = cleaned_data.get("employees")
        if start_date and end_date:
            if end_date < start_date:
                self.add_error("end_date", _("End date must be on or after start date."))
            if end_date > timezone.localdate():
                self.add_error("end_date", _("A payroll run cannot include future dates."))
        if company and employees:
            wrong_company = [
                employee
                for employee in employees
                if getattr(
                    getattr(
                        getattr(employee, "employee_work_info", None),
                        "company_id",
                        None,
                    ),
                    "pk",
                    None,
                )
                != company.pk
            ]
            if wrong_company:
                self.add_error(
                    "employees",
                    _("Every selected employee must work for the selected company."),
                )
        return cleaned_data


def _authorized(user, permission):
    return user.is_superuser or user.has_perm(permission)


def _require_permission(request, permission):
    if not request.user.is_authenticated:
        return False
    return _authorized(request.user, permission)


def _run_queryset():
    return PayrollRun.objects.select_related("company", "created_by")


def _get_run_or_404(run_id):
    return get_object_or_404(_run_queryset(), pk=run_id)


def _record_event(run, actor, from_status, to_status, comment=""):
    PayrollApprovalEvent.objects.create(
        payroll_run=run,
        actor=actor,
        from_status=from_status,
        to_status=to_status,
        comment=comment,
    )


def _run_transition_error(status, action):
    expected_status = RUN_ACTION_SOURCE_STATUS.get(action)
    if expected_status is None:
        return _("Unknown payroll action.")
    if status != expected_status:
        return _("Cannot %(action)s a payroll run in %(status)s state.") % {
            "action": action,
            "status": status,
        }
    return None


@login_required
def payroll_run_list(request):
    if not (
        _require_permission(request, "payroll.view_payslip")
        or _authorized(request.user, "payroll.add_payslip")
    ):
        return HttpResponseForbidden(_("You do not have permission to view payroll runs."))
    return render(
        request,
        "payroll/run_list.html",
        {"payroll_runs": _run_queryset()[:100]},
    )


@login_required
def payroll_run_create(request):
    if not _require_permission(request, "payroll.add_payslip"):
        return HttpResponseForbidden(_("You do not have permission to create payroll runs."))
    form = PayrollRunForm(request.POST or None, user=request.user, request=request)
    if request.method == "POST" and form.is_valid():
        company = form.cleaned_data["company"]
        start_date = form.cleaned_data["start_date"]
        end_date = form.cleaned_data["end_date"]
        employees = list(form.cleaned_data["employees"])
        try:
            with transaction.atomic():
                existing_runs = PayrollRun._base_manager.select_for_update().filter(
                    company=company, start_date=start_date, end_date=end_date
                )
                latest_run = existing_runs.order_by("-attempt").first()
                if latest_run and latest_run.status != "rejected":
                    form.add_error(
                        None,
                        _("A non-rejected payroll run already exists for this company and period."),
                    )
                    raise ValidationError(_("Duplicate payroll run."))
                attempt = (existing_runs.aggregate(max_attempt=Max("attempt"))[
                    "max_attempt"
                ] or 0) + 1
                run = PayrollRun.objects.create(
                    company=company,
                    start_date=start_date,
                    end_date=end_date,
                    attempt=attempt,
                    created_by=request.user,
                )
                _record_event(run, request.user, "", "draft", _("Preview created."))

                warnings = []
                for employee in employees:
                    contract = (
                        employee.contract_set.filter(
                            contract_status="active",
                            contract_start_date__lte=end_date,
                        )
                        .filter(
                            Q(contract_end_date__isnull=True)
                            | Q(contract_end_date__gte=start_date)
                        )
                        .order_by("-contract_start_date")
                        .first()
                    )
                    if contract is None:
                        warnings.append(
                            _("%(employee)s has no active contract for this period.")
                            % {"employee": employee}
                        )
                        continue

                    item_start = max(start_date, contract.contract_start_date)
                    existing_payslip = Payslip._base_manager.filter(
                        employee_id=employee,
                        start_date=item_start,
                        end_date=end_date,
                    ).first()
                    is_employee_estimate = (
                        existing_payslip is not None
                        and existing_payslip.status == "draft"
                        and isinstance(existing_payslip.pay_head_data, dict)
                        and existing_payslip.pay_head_data.get("snapshot_source")
                        == "employee_dashboard_estimate"
                    )
                    if existing_payslip is not None and not is_employee_estimate:
                        warnings.append(
                            _("%(employee)s already has a payslip for this period.")
                            % {"employee": employee}
                        )
                        continue

                    try:
                        from payroll.views.component_views import (
                            calculate_employer_contribution,
                            payroll_calculation,
                        )

                        calculation = payroll_calculation(
                            employee, item_start, end_date
                        )
                    except (ArithmeticError, KeyError, TypeError, ValueError, ValidationError) as exc:
                        warnings.append(
                            _("%(employee)s could not be calculated: %(error)s")
                            % {"employee": employee, "error": str(exc)}
                        )
                        continue

                    pay_data = json.loads(calculation["json_data"])
                    contribution_data = {"pay_data": pay_data}
                    calculate_employer_contribution(contribution_data)
                    pay_data = contribution_data["pay_data"]
                    pay_data["snapshot_source"] = "official_payroll_calculation"
                    item_warnings = []
                    if calculation["net_pay"] < 0:
                        item_warnings.append(
                            _("Calculated net pay is negative; correct the discrepancy before submission.")
                        )
                    installment_ids = [
                        installment.pk for installment in calculation["installments"]
                    ]
                    snapshot = {
                        "pay_data": pay_data,
                        "installment_ids": installment_ids,
                        "group_name": f"{start_date:%Y-%m}",
                    }
                    PayrollRunItem.objects.create(
                        payroll_run=run,
                        employee=employee,
                        start_date=item_start,
                        end_date=end_date,
                        basic_pay=float(calculation["basic_pay"]),
                        gross_pay=float(calculation["gross_pay"]),
                        deduction=float(calculation["total_deductions"]),
                        net_pay=float(calculation["net_pay"]),
                        snapshot=snapshot,
                        warnings=item_warnings,
                    )
                    warnings.extend(
                        _("%(employee)s: %(warning)s")
                        % {"employee": employee, "warning": warning}
                        for warning in item_warnings
                    )

                run.warnings = [str(warning) for warning in warnings]
                if warnings:
                    run.status = "rejected"
                    run.error_summary = "\n".join(run.warnings)
                    run.save(update_fields=["warnings", "status", "error_summary", "updated_at"])
                    _record_event(
                        run,
                        request.user,
                        "draft",
                        "rejected",
                        _("Preview blocked by validation warnings."),
                    )
                elif not run.items.exists():
                    run.status = "rejected"
                    run.error_summary = str(_("No eligible payroll calculations were produced."))
                    run.save(update_fields=["status", "error_summary", "updated_at"])
                    _record_event(
                        run,
                        request.user,
                        "draft",
                        "rejected",
                        run.error_summary,
                    )
        except IntegrityError:
            form.add_error(None, _("A payroll run was created concurrently. Refresh and try again."))
        except ValidationError as exc:
            if not form.non_field_errors():
                form.add_error(None, exc)
        else:
            messages.success(
                request,
                _("Payroll preview created. Review the calculation snapshot before submission."),
            )
            notify_payroll_admins(
                request.user,
                run.company,
                _("Payroll run for %(company)s (%(start)s–%(end)s) was created.")
                % {
                    "company": run.company,
                    "start": run.start_date,
                    "end": run.end_date,
                },
                redirect=payroll_run_notification_url(run),
                icon="calculator-outline",
            )
            return redirect("payroll-run-detail", run_id=run.pk)

    return render(request, "payroll/run_form.html", {"form": form})


@login_required
def payroll_run_detail(request, run_id):
    if not (
        _require_permission(request, "payroll.view_payslip")
        or _authorized(request.user, "payroll.add_payslip")
    ):
        return HttpResponseForbidden(_("You do not have permission to view payroll runs."))
    run = _get_run_or_404(run_id)
    return render(
        request,
        "payroll/run_detail.html",
        {
            "run": run,
            "items": run.items.select_related("employee", "payslip").all(),
            "events": run.approval_events.select_related("actor").all(),
        },
    )


@login_required
def payroll_run_action(request, run_id, action):
    if request.method != "POST":
        return HttpResponseForbidden(_("Payroll run transitions require POST."))
    permission_by_action = {
        "submit": "payroll.submit_payrollrun",
        "approve": "payroll.approve_payrollrun",
        "reject": "payroll.approve_payrollrun",
        "finalize": "payroll.finalize_payrollrun",
    }
    required_permission = permission_by_action.get(action)
    if required_permission is None or not _require_permission(request, required_permission):
        return HttpResponseForbidden(_("You do not have permission for this payroll action."))

    comment = request.POST.get("comment", "").strip()
    if action == "reject" and not comment:
        messages.error(request, _("A rejection reason is required."))
        return redirect("payroll-run-detail", run_id=run_id)

    try:
        with transaction.atomic():
            run = get_object_or_404(
                _run_queryset().select_related(None).select_for_update(),
                pk=run_id,
            )
            items = list(
                run.items.select_for_update()
                .order_by("employee_id")
            )
            previous_status = run.status
            transition_error = _run_transition_error(run.status, action)
            if transition_error:
                raise ValidationError(transition_error)

            if action == "submit":
                if run.status != "draft" or not items or run.warnings:
                    raise ValidationError(
                        _("Only a warning-free draft with calculated items can be submitted.")
                    )
                for item in items:
                    existing_payslip = Payslip._base_manager.select_for_update().filter(
                        employee_id=item.employee,
                        start_date=item.start_date,
                        end_date=item.end_date,
                    ).first()
                    if existing_payslip is not None and not (
                        existing_payslip.status == "draft"
                        and isinstance(existing_payslip.pay_head_data, dict)
                        and existing_payslip.pay_head_data.get("snapshot_source")
                        == "employee_dashboard_estimate"
                    ):
                        raise ValidationError(
                            _("A payslip now exists for %(employee)s; this run was not submitted.")
                            % {"employee": item.employee}
                        )
                for item in items:
                    snapshot = item.snapshot
                    pay_data = snapshot["pay_data"]
                    payslip = (
                        Payslip._base_manager.select_for_update()
                        .filter(
                            employee_id=item.employee,
                            start_date=item.start_date,
                            end_date=item.end_date,
                        )
                        .first()
                    )
                    if payslip is None:
                        payslip = Payslip(
                            employee_id=item.employee,
                            start_date=item.start_date,
                            end_date=item.end_date,
                        )
                    payslip.payroll_run = run
                    payslip.pay_head_data = pay_data
                    payslip.contract_wage = pay_data["contract_wage"]
                    payslip.basic_pay = item.basic_pay
                    payslip.gross_pay = item.gross_pay
                    payslip.deduction = item.deduction
                    payslip.net_pay = item.net_pay
                    payslip.status = "draft"
                    payslip.group_name = snapshot["group_name"]
                    payslip.save()
                    payslip.installment_ids.set(snapshot["installment_ids"])
                    payslip.status = "review_ongoing"
                    payslip.save(update_fields=["status"])
                    item.payslip = payslip
                    item.save(update_fields=["payslip"])
                run.status = "submitted"
            elif action in {"approve", "reject"}:
                if run.status != "submitted" or not items or any(
                    item.payslip is None for item in items
                ):
                    raise ValidationError(_("Only a submitted run can be reviewed."))
                if action == "approve":
                    for item in items:
                        item.payslip.status = "confirmed"
                        item.payslip.save(update_fields=["status"])
                    run.status = "approved"
                else:
                    for item in items:
                        item.payslip.status = "draft"
                        item.payslip.save(update_fields=["status"])
                    run.status = "rejected"
                    run.error_summary = comment
            elif action == "finalize":
                if run.status != "approved" or not items or any(
                    item.payslip is None or item.payslip.status != "confirmed"
                    for item in items
                ):
                    raise ValidationError(_("Only an approved, unchanged run can be finalized."))
                run.status = "finalized"
            run.save(update_fields=["status", "error_summary", "updated_at"])
            _record_event(run, request.user, previous_status, run.status, comment)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    except IntegrityError:
        messages.error(request, _("The payroll operation conflicted with another update. No changes were saved."))
    else:
        messages.success(request, _("Payroll run %(status)s.") % {"status": run.get_status_display()})
        notify_payroll_admins(
            request.user,
            run.company,
            _("Payroll run for %(company)s (%(start)s–%(end)s) is now %(status)s.")
            % {
                "company": run.company,
                "start": run.start_date,
                "end": run.end_date,
                "status": run.get_status_display(),
            },
            redirect=payroll_run_notification_url(run),
            icon="cash-outline",
        )
        if action in {"submit", "approve", "finalize"}:
            employee_verb = {
                "submit": _("A payslip for %(start)s–%(end)s is ready for review."),
                "approve": _("Your payslip for %(start)s–%(end)s has been approved."),
                "finalize": _("Your payroll for %(start)s–%(end)s has been finalized."),
            }[action] % {"start": run.start_date, "end": run.end_date}
            for item in items:
                recipient = item.employee.employee_user_id
                if recipient is None:
                    continue
                send_payroll_notification(
                    request.user,
                    recipient=recipient,
                    verb=employee_verb,
                    redirect=reverse("payroll-dashboard"),
                    icon="cash-outline",
                )
    return redirect("payroll-run-detail", run_id=run_id)
