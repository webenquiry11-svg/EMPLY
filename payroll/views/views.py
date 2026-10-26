"""
views.py

This module is used to define the method for the path in the urls
"""

import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from itertools import groupby
from urllib.parse import parse_qs

import pandas as pd
import pdfkit
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.context_processors import PermWrapper
from django.contrib.auth.decorators import login_required as django_login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, ProtectedError, Q, Sum
from django.http import (
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
    HttpResponseNotAllowed,
    HttpResponseRedirect,
    JsonResponse,
)
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from swapper import load_model

from base.methods import (
    closest_numbers,
    eval_validate,
    export_data,
    generate_colors,
    generate_pdf,
    get_key_instances,
    get_working_days,
    filter_own_records,
    sortby,
)
from base.models import Company
from employee.models import Employee, EmployeeWorkInformation
from horilla.decorators import (
    hx_request_required,
    login_required,
    owner_can_enter,
    permission_required,
)
from horilla.group_by import group_by_queryset
from horilla.horilla_settings import HORILLA_DATE_FORMATS
from horilla.http.response import HorillaRedirect
from payroll.notifications import (
    notify_payroll_admins,
    notify_payslip_status,
    send_payroll_notification,
)
from payroll.context_processors import get_active_employees
from payroll.services import (
    calculate_employee_payroll,
    create_current_payslip,
    employee_payslips,
    monthly_period,
)
from payroll.filters import (
    ContractFilter,
    ContractReGroup,
    PayslipFilter,
)
from payroll.forms.component_forms import (
    ContractExportFieldForm,
    PayrollSettingsForm,
    PayslipAutoGenerateForm,
)
from payroll.methods.methods import get_daily_salary, paginator_qry, save_payslip
from payroll.models.models import (
    Allowance,
    Contract,
    Deduction,
    FilingStatus,
    PayrollGeneralSetting,
    Payslip,
    PayslipAutoGenerate,
    PayrollRun,
    Reimbursement,
    ReimbursementFile,
    ReimbursementrequestComment,
)
from payroll.models.tax_models import PayrollSettings

# Create your views here.

status_choices = {
    "draft": _("Draft"),
    "review_ongoing": _("Review Ongoing"),
    "confirmed": _("Confirmed"),
    "paid": _("Paid"),
}

Notification = load_model("notifications", "Notification")


@django_login_required
def payroll_notification_feed(request):
    """Return the signed-in user's payroll notifications only."""
    notifications = request.user.notifications.filter(
        data__payroll_event=True,
        deleted=False,
    ).order_by("-timestamp")[:10]
    unread_count = request.user.notifications.filter(
        data__payroll_event=True,
        unread=True,
        deleted=False,
    ).count()
    items = []
    for notification in notifications:
        data = notification.data or {}
        target_url = data.get("redirect", "")
        if not isinstance(target_url, str) or not target_url.startswith("/") or target_url.startswith("//"):
            target_url = ""
        items.append(
            {
                "id": notification.pk,
                "verb": notification.verb,
                "timestamp": notification.timestamp.isoformat(),
                "unread": notification.unread,
                "url": target_url,
                "icon": data.get("icon", "notifications-outline"),
            }
        )
    return JsonResponse({"unread_count": unread_count, "notifications": items})


@django_login_required
def mark_payroll_notification_read(request, notification_id):
    """Mark one of the current user's payroll notifications as read."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    notification = request.user.notifications.filter(
        pk=notification_id,
        data__payroll_event=True,
        deleted=False,
    ).first()
    if notification is None:
        return JsonResponse(
            {"success": False, "error": _("Notification not found.")},
            status=404,
        )
    notification.mark_as_read()
    return JsonResponse({"success": True})

PAYSLIP_STATUS_TRANSITIONS = {
    "draft": {"review_ongoing"},
    "review_ongoing": {"draft", "confirmed"},
    "confirmed": {"paid"},
    "paid": set(),
}


def _payslip_status_error(payslip, new_status):
    if new_status not in status_choices:
        return _("Invalid payslip status.")
    if new_status == payslip.status:
        return None
    if new_status not in PAYSLIP_STATUS_TRANSITIONS.get(payslip.status, set()):
        return _("This payslip cannot transition from %(old)s to %(new)s.") % {
            "old": payslip.get_status_display(),
            "new": status_choices[new_status],
        }
    return None


def get_language_code(request):
    scale_x_text = _("Name of Employees")
    scale_y_text = _("Amount")
    response = {"scale_x_text": scale_x_text, "scale_y_text": scale_y_text}
    return JsonResponse(response)


@login_required
@permission_required("payroll.add_contract")
def contract_create(request):
    """
    Contract create view
    """
    from payroll.forms.forms import ContractForm

    form = ContractForm()
    if request.method == "POST":
        form = ContractForm(request.POST, request.FILES)
        if form.is_valid():
            form.save()
            messages.success(request, _("Contract Created"))
            return redirect(contract_view)
    return render(request, "payroll/common/form.html", {"form": form})


@login_required
@permission_required("payroll.change_contract")
def contract_update(request, contract_id, **kwargs):
    """
    Update an existing contract.

    Args:
        request: The HTTP request object.
        contract_id: The ID of the contract to update.

    Returns:
        If the request method is POST and the form is valid, redirects to the contract view.
        Otherwise, renders the contract update form.

    """
    from payroll.forms.forms import ContractForm

    contract = Contract.objects.filter(id=contract_id).first()
    if not contract:
        messages.info(request, _("The contract could not be found."))
        return redirect(contract_view)
    contract_form = ContractForm(instance=contract)
    if request.method == "POST":
        contract_form = ContractForm(request.POST, request.FILES, instance=contract)
        if contract_form.is_valid():
            contract_form.save()
            messages.success(request, _("Contract updated"))
            return redirect(contract_view)
    return render(
        request,
        "payroll/common/form.html",
        {
            "form": contract_form,
        },
    )


@login_required
@hx_request_required
@permission_required("payroll.change_contract")
def contract_status_update(request, contract_id):
    from payroll.forms.forms import ContractForm

    previous_data = request.GET.urlencode()
    if request.method == "POST":
        contract = Contract.objects.get(id=contract_id)
        if request.POST.get("view"):
            status = request.POST.get("status")
            if status in dict(contract.CONTRACT_STATUS_CHOICES).keys():
                save = True
                if status in ["active", "draft"]:
                    active_contract = Contract.objects.filter(
                        contract_status="active", employee_id=contract.employee_id
                    ).exists()
                    draft_contract = Contract.objects.filter(
                        contract_status="draft", employee_id=contract.employee_id
                    ).exists()
                    if (status == "active" and active_contract) or (
                        status == "draft" and draft_contract
                    ):
                        save = False
                        messages.info(
                            request,
                            _("An {} contract already exists for {}").format(
                                status, contract.employee_id
                            ),
                        )
                if save:
                    contract.contract_status = status
                    contract.save()
                    messages.success(
                        request, _("The contract status has been updated successfully.")
                    )
            else:
                messages.warning(
                    request, _("You selected the wrong option for contract status.")
                )

            return redirect(f"/payroll/contract-filter?{previous_data}")

        contract_form = ContractForm(request.POST, request.FILES, instance=contract)
        if contract_form.is_valid():
            contract_form.save()
            messages.success(request, _("Contract status updated"))
        else:
            for errors in contract_form.errors.values():
                for error in errors:
                    messages.error(request, error)
        return HttpResponse("<script>$('#reloadMessagesButton').click()</script>")


@login_required
@permission_required("payroll.change_contract")
def bulk_contract_status_update(request):
    status = request.POST.get("status")
    ids = eval_validate(request.POST.get("ids"))
    all_contracts = Contract.objects.all()
    contracts = all_contracts.filter(id__in=ids)

    for contract in contracts:
        save = True
        if status in ["active", "draft"]:
            active_contract = all_contracts.filter(
                contract_status="active", employee_id=contract.employee_id
            ).exists()
            draft_contract = all_contracts.filter(
                contract_status="draft", employee_id=contract.employee_id
            ).exists()
            if (status == "active" and active_contract) or (
                status == "draft" and draft_contract
            ):
                save = False
                messages.info(
                    request,
                    _("An {} contract already exists for {}").format(
                        status, contract.employee_id
                    ),
                )
        if save:
            contract.contract_status = status
            contract.save()
            messages.success(
                request, _("The contract status has been updated successfully.")
            )
    return HttpResponse("success")


@login_required
@permission_required("payroll.change_contract")
def update_contract_filing_status(request, contract_id):
    if request.method == "POST":
        contract = get_object_or_404(Contract, id=contract_id)
        filing_status_id = request.POST.get("filing_status")
        try:
            filing_status = (
                FilingStatus.objects.get(id=int(filing_status_id))
                if filing_status_id
                else None
            )
            contract.filing_status = filing_status
            messages.success(
                request, _("The employee filing status has been updated successfully.")
            )
        except (ValueError, OverflowError, FilingStatus.DoesNotExist):
            messages.warning(
                request, _("You selected the wrong option for filing status.")
            )
        contract.save()
        return redirect(contract_filter)


@login_required
@hx_request_required
@permission_required("payroll.delete_contract")
def contract_delete(request, contract_id):
    """
    Delete a contract.

    Args:
        contract_id: The ID of the contract to delete.

    Returns:
        Redirects to the contract view after successfully deleting the contract.

    """
    try:
        Contract.objects.get(id=contract_id).delete()
        messages.success(request, _("Contract deleted"))
        request_path = request.path.split("/")
        if "delete-contract-modal" in request_path:
            if instances_ids := request.GET.get("instances_ids"):
                get_data = request.GET.copy()
                get_data.pop("instances_ids", None)
                previous_data = get_data.urlencode()
                instances_list = json.loads(instances_ids)
                previous_instance, next_instance = closest_numbers(
                    instances_list, contract_id
                )
                if contract_id in instances_list:
                    instances_list.remove(contract_id)
                urls = f"/payroll/single-contract-view/{next_instance}/"
                params = f"?{previous_data}&instances_ids={instances_list}"
                return redirect(urls + params)
            return HorillaRedirect(request)
        else:
            return redirect(f"/payroll/contract-filter?{request.GET.urlencode()}")
    except Contract.DoesNotExist:
        messages.error(request, _("Contract not found."))
    except ProtectedError:
        messages.error(request, _("You cannot delete this contract."))
    return HorillaRedirect(request)


@login_required
@permission_required("payroll.view_contract")
def contract_view(request):
    """
    Contract view method
    """

    contracts = Contract.objects.all()
    if contracts.exists():
        template = "payroll/contract/contract_view.html"
    else:
        template = "payroll/contract/contract_empty.html"

    contracts = paginator_qry(contracts, request.GET.get("page"))
    contract_ids_json = json.dumps([instance.id for instance in contracts.object_list])
    filter_form = ContractFilter(request.GET)
    context = {
        "contracts": contracts,
        "f": filter_form,
        "contract_ids": contract_ids_json,
        "gp_fields": ContractReGroup.fields,
    }

    return render(request, template, context)


@login_required
# @hx_request_required         #this function is also used in payroll dashboard which uses ajax
@owner_can_enter("payroll.view_contract", Contract)
def view_single_contract(request, contract_id):
    """
    Renders a single contract view page.
    """
    get_data = request.GET.copy()
    get_data.pop("instances_ids", None)
    previous_data = get_data.urlencode()
    dashboard = request.GET.get("dashboard", "")

    HTTP_REFERERS = request.META.get("HTTP_REFERER", "").split("/")
    delete_hx_target = (
        "#personal_target"
        if "employee-view" in HTTP_REFERERS or "employee-profile" in HTTP_REFERERS
        else "#objectDetailsModalTarget"
    )

    contract = Contract.find(contract_id)

    context = {
        "contract": contract,
        "dashboard": dashboard,
        "delete_hx_target": delete_hx_target,
        "pd": previous_data,
    }

    contract_ids_json = request.GET.get("instances_ids")
    if contract_ids_json:
        contract_ids = json.loads(contract_ids_json)
        previous_id, next_id = closest_numbers(contract_ids, contract_id)
        context.update(
            {
                "previous": previous_id,
                "next": next_id,
                "contract_ids": contract_ids_json,
            }
        )
    return render(request, "payroll/contract/contract_single_view.html", context)


@login_required
@hx_request_required
@permission_required("payroll.view_contract")
def contract_filter(request):
    """
    Filter contracts based on the provided query parameters.

    Args:
        request: The HTTP request object containing the query parameters.

    Returns:
        Renders the contract list template with the filtered contracts.

    """
    query_string = request.GET.urlencode()
    contracts_filter = ContractFilter(request.GET)
    template = "payroll/contract/contract_list.html"
    contracts = contracts_filter.qs
    field = request.GET.get("field")

    if field != "" and field is not None:
        contracts = group_by_queryset(contracts, field, request.GET.get("page"), "page")
        list_values = [entry["list"] for entry in contracts]
        id_list = []
        for value in list_values:
            for instance in value.object_list:
                id_list.append(instance.id)

        contract_ids_json = json.dumps(list(id_list))
        template = "payroll/contract/group_by.html"

    else:
        contracts = sortby(request, contracts, "orderby")
        contracts = paginator_qry(contracts, request.GET.get("page"))
        contract_ids_json = json.dumps(
            [instance.id for instance in contracts.object_list]
        )

    data_dict = parse_qs(query_string)
    get_key_instances(Contract, data_dict)
    keys_to_remove = [key for key, value in data_dict.items() if value == ["unknown"]]
    for key in keys_to_remove:
        data_dict.pop(key)
    if "contract_status" in data_dict:
        status_list = data_dict["contract_status"]
        if len(status_list) > 1:
            data_dict["contract_status"] = [status_list[-1]]
    return render(
        request,
        template,
        {
            "contracts": contracts,
            "pd": query_string,
            "filter_dict": data_dict,
            "contract_ids": contract_ids_json,
            "field": field,
        },
    )


@login_required
@permission_required("payroll.view_payrollsettings")
def settings(request):
    """
    This method is used to render settings template
    """
    instance = PayrollSettings.objects.first()
    currency_form = PayrollSettingsForm(instance=instance)
    selected_company_id = request.session.get("selected_company")

    if selected_company_id == "all" or not selected_company_id:
        companies = Company.objects.all()
    else:
        companies = Company.objects.filter(id=selected_company_id)

    if request.method == "POST":

        currency_form = PayrollSettingsForm(request.POST, instance=instance)
        if currency_form.is_valid():

            currency_form.save()
            messages.success(request, _("Payroll settings updated."))
            return HorillaRedirect(request)
    return render(
        request,
        "payroll/settings/payroll_settings.html",
        {
            "currency_form": currency_form,
            "companies": companies,
            "selected_company_id": selected_company_id,
        },
    )


@login_required
@permission_required("payroll.change_payslip")
def update_payslip_status(request, payslip_id):
    """
    This method is used to update the payslip confirmation status
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    new_status = request.POST.get("status")
    view = request.POST.get("view")
    with transaction.atomic():
        payslip = get_object_or_404(
            Payslip.objects.select_for_update(), id=payslip_id
        )
        previous_status = payslip.status
        error = _payslip_status_error(payslip, new_status)
        if error:
            messages.error(request, error)
            if view:
                from .component_views import filter_payslip

                return redirect(filter_payslip)
            data = payslip.pay_head_data
            data["employee"] = payslip.employee_id
            data["payslip"] = payslip
            data["json_data"] = data.copy()
            data["json_data"]["employee"] = payslip.employee_id.id
            data["json_data"]["payslip"] = payslip.id
            data["instance"] = payslip
            return render(
                request,
                "payroll/payslip/individual_payslip_summery.html",
                data,
                status=400,
            )
        try:
            if new_status != payslip.status:
                payslip.status = new_status
                payslip.save(update_fields=["status"])
            messages.success(request, _("Payslip status updated"))
        except ValidationError as exc:
            return HttpResponse("; ".join(exc.messages), status=400)
    notify_payslip_status(request.user, payslip, previous_status)
    if view:
        from .component_views import filter_payslip

        return redirect(filter_payslip)
    data = payslip.pay_head_data
    data["employee"] = payslip.employee_id
    data["payslip"] = payslip
    data["json_data"] = data.copy()
    data["json_data"]["employee"] = payslip.employee_id.id
    data["json_data"]["payslip"] = payslip.id
    data["instance"] = payslip
    return render(request, "payroll/payslip/individual_payslip_summery.html", data)


def update_payslip_status_no_id(request):
    """
    This method is used to update the payslip confirmation status
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not request.user.is_authenticated or not request.user.has_perm(
        "payroll.change_payslip"
    ):
        return JsonResponse(
            {"type": "error", "message": _("You do not have permission.")},
            status=403,
        )
    try:
        ids = json.loads(request.POST.get("ids", ""))
    except (TypeError, ValueError):
        return JsonResponse(
            {"type": "error", "message": _("Invalid payslip selection.")}, status=400
        )
    if (
        not isinstance(ids, list)
        or not ids
        or any(
            isinstance(pk, bool)
            or not str(pk).isdigit()
            or int(pk) <= 0
            for pk in ids
        )
        or len({int(pk) for pk in ids}) != len(ids)
    ):
        return JsonResponse(
            {"type": "error", "message": _("Invalid payslip selection.")}, status=400
        )
    ids = [int(pk) for pk in ids]
    new_status = request.POST.get("status")
    changed_slips = []
    try:
        with transaction.atomic():
            slips = list(
                Payslip.objects.select_for_update().filter(id__in=ids).order_by("id")
            )
            if len(slips) != len(ids):
                return JsonResponse(
                    {
                        "type": "error",
                        "message": _("One or more payslips were not found."),
                    },
                    status=404,
                )
            for payslip in slips:
                error = _payslip_status_error(payslip, new_status)
                if error:
                    return JsonResponse(
                        {"type": "error", "message": error}, status=400
                    )
            for payslip in slips:
                if payslip.status != new_status:
                    previous_status = payslip.status
                    payslip.status = new_status
                    payslip.save(update_fields=["status"])
                    changed_slips.append((payslip, previous_status))
    except ValidationError as exc:
        return JsonResponse(
            {"type": "error", "message": "; ".join(exc.messages)}, status=400
        )
    for payslip, previous_status in changed_slips:
        notify_payslip_status(request.user, payslip, previous_status)
    return JsonResponse(
        {
            "type": "success",
            "message": _("%(count)s payslip statuses updated.")
            % {"count": len(slips)},
        }
    )


@login_required
@permission_required("payroll.change_payslip")
def bulk_update_payslip_status(request):
    """
    This method is used to update payslip status when generating payslip through
    generate payslip method
    """
    try:
        pay_data = json.loads(request.GET.get("json_data", ""))
    except (TypeError, ValueError):
        return JsonResponse(
            {"type": "error", "message": _("Invalid payroll data.")}, status=400
        )
    new_status = request.GET.get("status")
    if (
        not isinstance(pay_data, list)
        or not pay_data
        or new_status not in {"draft", "review_ongoing"}
    ):
        return JsonResponse(
            {"type": "error", "message": _("Invalid payroll data or status.")},
            status=400,
        )

    saved_statuses = []
    try:
        with transaction.atomic():
            for json_entry in pay_data:
                data = json.loads(json_entry) if isinstance(json_entry, str) else json_entry
                if not isinstance(data, dict):
                    raise ValueError("Each payslip entry must be an object.")
                employee = Employee.objects.filter(id=data.get("employee")).first()
                if employee is None:
                    raise ValidationError(
                        _("An employee was not found in your company scope.")
                    )
                payslip_kwargs = {
                    "employee_id": employee,
                    "start_date": data["start_date"],
                    "end_date": data["end_date"],
                }
                instance = Payslip.objects.select_for_update().filter(
                    **payslip_kwargs
                ).first()
                previous_status = instance.status if instance is not None else None
                if instance is None:
                    instance = Payslip()
                else:
                    error = _payslip_status_error(instance, new_status)
                    if error:
                        raise ValidationError(error)
                instance.employee_id = employee
                instance.start_date = data["start_date"]
                instance.end_date = data["end_date"]
                instance.status = new_status
                instance.basic_pay = data["basic_pay"]
                instance.contract_wage = data["contract_wage"]
                instance.gross_pay = data["gross_pay"]
                instance.deduction = data["total_deductions"]
                instance.net_pay = data["net_pay"]
                instance.pay_head_data = data
                instance.save()
                saved_statuses.append((instance, previous_status))
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        error_message = (
            "; ".join(exc.messages)
            if isinstance(exc, ValidationError)
            else _("Invalid payroll data.")
        )
        return JsonResponse({"type": "error", "message": error_message}, status=400)

    for payslip, previous_status in saved_statuses:
        notify_payslip_status(request.user, payslip, previous_status)
    return JsonResponse({"type": "success", "message": _("Payslips saved.")})


@django_login_required
def view_payslip_pdf(request, payslip_id):

    from .component_views import filter_payslip

    if Payslip.objects.filter(id=payslip_id).exists():
        payslip = Payslip.objects.get(id=payslip_id)
        company = Company.objects.filter(hq=True).first()
        is_owner = payslip.employee_id.employee_user_id == request.user
        is_payroll_staff = request.user.has_perm("payroll.view_payslip")
        if is_owner or is_payroll_staff:
            user = request.user
            employee = getattr(user, "employee_get", None) or payslip.employee_id

            # Taking the company_name of the user
            info = EmployeeWorkInformation.objects.filter(employee_id=employee)
            date_format = "MMM. D, YYYY"
            if info.exists():
                for data in info:
                    employee_company = data.company_id
                company_name = Company.objects.filter(company=employee_company)
                emp_company = company_name.first()

                # Access the date_format attribute directly
                date_format = (
                    emp_company.date_format
                    if emp_company and emp_company.date_format
                    else "MMM. D, YYYY"
                )
            else:
                date_format = "MMM. D, YYYY"

            data = payslip.pay_head_data.copy()
            start_date_str = data["start_date"]
            end_date_str = data["end_date"]

            # Convert the string to a datetime.date object
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
            end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()

            if data.get("unpaid_days") is None and data.get("loss_of_pay") is not None:
                daily_salary = get_daily_salary(
                    data.get("contract_wage") or payslip.contract_wage or 0,
                    start_date,
                )["day_wage"]
                if daily_salary:
                    data["unpaid_days"] = round(
                        float(data["loss_of_pay"]) / daily_salary, 2
                    )
            if data.get("paid_days") is None and data.get("unpaid_days") is not None:
                working_days = get_working_days(start_date, end_date)[
                    "total_working_days"
                ]
                data["paid_days"] = round(
                    working_days - float(data["unpaid_days"]), 2
                )

            month_start_name = start_date.strftime("%B %d, %Y")
            month_end_name = end_date.strftime("%B %d, %Y")

            # Formatted date for each format
            format_string = HORILLA_DATE_FORMATS.get(date_format, "%b. %d, %Y")
            formatted_start_date = start_date.strftime(format_string)
            formatted_end_date = end_date.strftime(format_string)
            data["month_start_name"] = month_start_name
            data["month_end_name"] = month_end_name
            data["formatted_start_date"] = formatted_start_date
            data["formatted_end_date"] = formatted_end_date
            data["employee"] = payslip.employee_id
            data["payslip"] = payslip
            data["json_data"] = data.copy()
            data["json_data"]["employee"] = payslip.employee_id.id
            data["json_data"]["payslip"] = payslip.id
            data["instance"] = payslip
            data["currency"] = (
                PayrollSettings.objects.first().currency_symbol
                if PayrollSettings.objects.exists()
                else "₹"
            )
            data["all_deductions"] = []
            for deduction_list in [
                data["basic_pay_deductions"],
                data["gross_pay_deductions"],
                data["pretax_deductions"],
                data["post_tax_deductions"],
                data["tax_deductions"],
                data["net_deductions"],
            ]:
                data["all_deductions"].extend(deduction_list)

            data["all_allowances"] = data["allowances"].copy()
            equalize_lists_length(data["allowances"], data["all_deductions"])
            data["zipped_data"] = zip(data["allowances"], data["all_deductions"])
            data["host"] = request.get_host()
            data["protocol"] = "https" if request.is_secure() else "http"
            data["company"] = company

            return render(request, "payroll/payslip/employee_payslip.html", context=data)
        return redirect(filter_payslip)
    return render(request, "405.html")


@login_required
# @permission_required("payroll.view_payslip")
def view_created_payslip(request, payslip_id, **kwargs):
    """
    This method is used to view the saved payslips
    """
    payslip = Payslip.objects.filter(id=payslip_id).first()
    if payslip is not None and (
        request.user.has_perm("payroll.view_payslip")
        or payslip.employee_id.employee_user_id == request.user
    ):
        # the data must be dictionary in the payslip model for the json field
        data = payslip.pay_head_data
        data["employee"] = payslip.employee_id
        data["payslip"] = payslip
        data["json_data"] = data.copy()
        data["json_data"]["employee"] = payslip.employee_id.id
        data["json_data"]["payslip"] = payslip.id
        data["instance"] = payslip
        return render(request, "payroll/payslip/individual_payslip.html", data)
    return render(request, "404.html")


@login_required
@permission_required("payroll.delete_payslip")
def delete_payslip(request, payslip_id):
    """
    This method is used to delete payslip instances
    Args:
        payslip_id (int): Payslip model instance id
    """
    from .component_views import filter_payslip

    try:
        Payslip.objects.get(id=payslip_id).delete()
        messages.success(request, _("Payslip deleted"))
    except Payslip.DoesNotExist:
        messages.error(request, _("Payslip not found."))
    except ProtectedError:
        messages.error(request, _("Something went wrong"))
    if not Payslip.objects.filter():
        return HorillaRedirect(request)
    return redirect(filter_payslip)


@login_required
@permission_required("payroll.add_contract")
def contract_info_initial(request):
    """
    This is an ajax method to return json response to auto fill the contract
    form fields
    """
    employee_id = request.GET["employee_id"]
    work_info = EmployeeWorkInformation.objects.filter(employee_id=employee_id).first()
    response_data = {
        "department": (
            work_info.department_id.id if work_info.department_id is not None else ""
        ),
        "job_position": (
            work_info.job_position_id.id
            if work_info.job_position_id is not None
            else ""
        ),
        "job_role": (
            work_info.job_role_id.id if work_info.job_role_id is not None else ""
        ),
        "shift": work_info.shift_id.id if work_info.shift_id is not None else "",
        "work_type": (
            work_info.work_type_id.id if work_info.work_type_id is not None else ""
        ),
        "wage": work_info.basic_salary,
        "contract_start_date": work_info.date_joining if work_info.date_joining else "",
        "contract_end_date": (
            work_info.contract_end_date if work_info.contract_end_date else ""
        ),
    }
    return JsonResponse(response_data)


@django_login_required
def employee_payroll_dashboard(request):
    """Render the authenticated employee's payroll dashboard."""

    employee = getattr(request.user, "employee_get", None)
    if (
        employee is None
        or not employee.is_active
        or getattr(employee, "is_suspended", False)
    ):
        return redirect("login")
    work_info = getattr(employee, "employee_work_info", None)
    salary_value = getattr(work_info, "basic_salary", None)
    salary_configured = salary_value is not None and salary_value > 0
    if not salary_configured:
        logout(request)
        messages.error(
            request,
            _(
                "Payroll access is unavailable because salary information "
                "has not been configured for your employee profile."
            ),
        )
        return redirect(f"{reverse('login')}?login_mode=payroll")

    payroll = calculate_employee_payroll(employee)
    payslips = list(employee_payslips(employee))
    current_period = payroll["start_date"].strftime("%Y-%m")
    payslip_by_period = {}
    finalized_by_period = {}
    for payslip in payslips:
        period = payslip.start_date.strftime("%Y-%m")
        payslip_by_period.setdefault(period, payslip)
        if payslip.status in ("confirmed", "paid"):
            finalized_by_period.setdefault(period, payslip)

    available_periods = set(payslip_by_period) | {current_period}
    period_options = {}
    for period in available_periods:
        payslip = finalized_by_period.get(period) or payslip_by_period.get(period)
        label = (
            payslip.start_date.strftime("%B %Y")
            if payslip
            else payroll["start_date"].strftime("%B %Y")
        )
        if period == current_period and period not in finalized_by_period:
            label += " · Estimate"
        elif period not in finalized_by_period and payslip:
            label += f" · {payslip.get_status_display()}"
        period_options[period] = label
    requested_period = request.GET.get("period", current_period)
    selected_period = (
        requested_period if requested_period in period_options else current_period
    )
    if selected_period == current_period:
        finalized_payslip = next(
            (
                payslip
                for payslip in payslips
                if payslip.start_date == payroll["start_date"]
                and payslip.end_date == payroll["end_date"]
                and payslip.status in ("confirmed", "paid")
            ),
            None,
        )
    else:
        finalized_payslip = finalized_by_period.get(selected_period)
    selected_payslip = finalized_payslip or (
        payslip_by_period.get(selected_period)
        if selected_period != current_period
        else None
    )
    selected_month = (
        selected_payslip.start_date.strftime("%B %Y")
        if selected_payslip
        else payroll["start_date"].strftime("%B %Y")
    )
    payroll_has_snapshot = selected_payslip is not None
    payroll_is_finalized = payroll_has_snapshot and selected_payslip.status in (
        "confirmed",
        "paid",
    )
    if payroll_has_snapshot:
        snapshot = selected_payslip.pay_head_data
        payroll["start_date"] = selected_payslip.start_date
        payroll["end_date"] = selected_payslip.end_date
        payroll["basic_salary"] = selected_payslip.basic_pay or 0
        payroll["current_salary"] = selected_payslip.basic_pay or 0
        payroll["gross_pay"] = selected_payslip.gross_pay or 0
        payroll["total_deductions"] = selected_payslip.deduction or 0
        payroll["net_pay"] = selected_payslip.net_pay or 0
        payroll["basic_salary_display"] = f"₹{payroll['basic_salary']:,.2f}"
        payroll["total_deductions_display"] = (
            f"₹{payroll['total_deductions']:,.2f}"
        )
        payroll["net_pay_display"] = f"₹{payroll['net_pay']:,.2f}"
        payroll["paid_days"] = snapshot.get("paid_days")
        payroll["unpaid_days"] = snapshot.get("unpaid_days")
        payroll["paid_leave_days"] = None
        payroll["unpaid_leave_days"] = None
        payroll["absence_days"] = None
        payroll["sandwich_days"] = None
        payroll["daily_rate_basis"] = (
            _("Amounts and days below are read from the finalized payslip snapshot.")
            if payroll_is_finalized
            else _("Amounts and days below are read from the stored draft payslip.")
        )
        payroll["deduction_details"] = []
        deduction_categories = (
            ("basic_pay_deductions", _("Basic pay deduction")),
            ("gross_pay_deductions", _("Gross pay deduction")),
            ("pretax_deductions", _("Pre-tax deduction")),
            ("post_tax_deductions", _("Post-tax deduction")),
            ("tax_deductions", _("Tax deduction")),
            ("net_deductions", _("Net pay deduction")),
        )
        for key, category_label in deduction_categories:
            for detail in snapshot.get(key, []) or []:
                amount = float(detail.get("amount", 0) or 0)
                payroll["deduction_details"].append(
                    {
                        "leave_type": detail.get("title")
                        or detail.get("deduction")
                        or category_label,
                        "reason": detail.get("title")
                        or detail.get("deduction")
                        or category_label,
                        "policy": category_label,
                        "amount_display": f"₹{amount:,.2f}",
                    }
                )
        payroll["total_affected_days"] = len(payroll["deduction_details"])
    period_options = [
        {"key": period, "label": label}
        for period, label in sorted(period_options.items(), reverse=True)
    ]
    for payslip in payslips:
        payslip.net_pay_display = (
            f"₹{payslip.net_pay:,.2f}"
            if payslip.net_pay is not None
            else _("Not available yet")
        )
        payslip.basic_pay_display = (
            f"₹{payslip.basic_pay:,.2f}"
            if payslip.basic_pay is not None
            else _("Not available yet")
        )
        payslip.deduction_display = (
            f"₹{payslip.deduction:,.2f}"
            if payslip.deduction is not None
            else _("Not available yet")
        )
    selected_month_payslips = [
        payslip
        for payslip in payslips
        if payslip.start_date.strftime("%Y-%m") == selected_period
    ]
    previous_month_date = payroll["start_date"] - timedelta(days=1)
    previous_payslip = next(
        (
            payslip
            for payslip in payslips
            if payslip.start_date.year == previous_month_date.year
            and payslip.start_date.month == previous_month_date.month
        ),
        None,
    )
    payroll_trends = {}
    if previous_payslip is not None:
        trend_values = (
            ("net_pay", payroll["net_pay"], previous_payslip.net_pay, "net_pay"),
            (
                "deductions",
                payroll["total_deductions"],
                previous_payslip.deduction,
                "deduction",
            ),
            (
                "earnings",
                payroll["basic_salary"],
                previous_payslip.basic_pay,
                "basic_pay",
            ),
        )
        for name, current_value, previous_value, field_name in trend_values:
            previous_amount = float(previous_value or 0)
            values = [
                float(getattr(payslip, field_name))
                for payslip in reversed(payslips)
                if payslip.start_date < payroll["start_date"]
                and getattr(payslip, field_name) is not None
            ] + [float(current_value)]
            spread = max(values) - min(values)
            points = []
            if len(values) > 1:
                points = [
                    f"{index * 40 / (len(values) - 1):.1f} "
                    f"{17 - ((value - min(values)) / spread * 14 if spread else 7):.1f}"
                    for index, value in enumerate(values)
                ]
            payroll_trends[name] = {
                "change": (
                    round(
                        (float(current_value) - previous_amount)
                        / abs(previous_amount)
                        * 100,
                        1,
                    )
                    if previous_amount
                    else None
                ),
                "has_change": bool(previous_amount),
                "sparkline": f"M {' L '.join(points)}" if len(points) > 1 else "",
            }
    salary_denominator = payroll["basic_salary"] or 1
    deduction_ratio = min(
        100,
        max(
            0,
            round(
                float(
                    payroll["total_deductions"] / salary_denominator * 100
                ),
                2,
            ),
        ),
    )
    net_pay_ratio = min(
        100,
        max(0, round(float(payroll["net_pay"] / salary_denominator * 100), 2)),
    )

    context = {
        "employee": employee,
        "employee_id": getattr(employee, "badge_id", ""),
        "employee_department": getattr(getattr(work_info, "department_id", None), "department", ""),
        "employee_designation": getattr(getattr(work_info, "job_position_id", None), "job_position", ""),
        "employee_name": getattr(employee, "employee_first_name", "")
        and f"{employee.employee_first_name} {employee.employee_last_name}".strip(),
        "salary_configured": salary_configured,
        "salary_value": salary_value,
        "current_period": selected_period,
        "current_month": selected_month,
        "period_options": period_options,
        "selected_period": selected_period,
        "payroll": payroll,
        "payroll_is_finalized": payroll_is_finalized,
        "payroll_has_snapshot": payroll_has_snapshot,
        "finalized_payslip": finalized_payslip,
        "selected_payslip": selected_payslip,
        "payslips": payslips,
        "selected_month_payslips": selected_month_payslips,
        "selected_is_current_period": selected_period == current_period,
        "employee_avatar": employee.get_avatar(),
        "deduction_ratio": deduction_ratio,
        "net_pay_ratio": net_pay_ratio,
        "payroll_trends": payroll_trends,
    }
    return render(request, "payroll/employee_dashboard.html", context=context)


@django_login_required
@permission_required("payroll.view_contract")
def payroll_management_dashboard(request):
    """Render the dedicated payroll management workspace."""
    valid_sections = {
        "dashboard",
        "payslips",
        "runs",
        "employees",
        "reports",
        "contracts",
        "allowances",
        "deductions",
        "reimbursements",
        "settings",
    }
    section = request.GET.get("section", "dashboard")
    if section not in valid_sections:
        section = "dashboard"
    section_permissions = {
        "payslips": "payroll.view_payslip",
        "employees": "employee.view_employee",
        "reports": "payroll.view_payslip",
        "contracts": "payroll.view_contract",
        "allowances": "payroll.view_allowance",
        "deductions": "payroll.view_deduction",
        "reimbursements": "payroll.view_reimbursement",
        "settings": "payroll.view_payrollsettings",
    }
    if (
        section in section_permissions
        and not request.user.is_superuser
        and not request.user.has_perm(section_permissions[section])
    ):
        return HttpResponseForbidden(_("You do not have access to this payroll section."))
    selected_period = request.GET.get("period", "")
    if selected_period:
        try:
            period_start = datetime.strptime(selected_period, "%Y-%m").date()
        except ValueError:
            return HttpResponseBadRequest(_("Select a valid payroll period."))
    else:
        period_start = timezone.localdate().replace(day=1)
        selected_period = period_start.strftime("%Y-%m")

    payslips = (
        Payslip._base_manager
        if request.user.is_superuser
        else Payslip.objects
    ).select_related(
        "employee_id", "employee_id__employee_work_info__department_id"
    )
    runs = (
        PayrollRun._base_manager
        if request.user.is_superuser
        else PayrollRun.objects
    ).select_related("company", "created_by")
    period_filter = {
        "start_date__year": period_start.year,
        "start_date__month": period_start.month,
    }
    month_payslips = payslips.filter(**period_filter)
    status_counts = {
        status: month_payslips.filter(status=status).count()
        for status, _label in Payslip.status_choices
    }
    summary = month_payslips.aggregate(
        total_net=Sum("net_pay"),
        total_deductions=Sum("deduction"),
        employee_count=Count("employee_id", distinct=True),
    )
    payslip_count = month_payslips.count()
    chart_statuses = [
        ("paid", _("Paid"), "#16b981"),
        ("confirmed", _("Confirmed"), "#3988fa"),
        ("review_ongoing", _("In review"), "#ffab21"),
        ("draft", _("Draft"), "#8798b2"),
    ]
    status_total = sum(status_counts.values())
    status_percent = {}
    assigned_percent = 0
    for index, (status, _label, _color) in enumerate(chart_statuses):
        if not status_total:
            percent = 0
        elif index == len(chart_statuses) - 1:
            percent = 100 - assigned_percent
        else:
            percent = int(status_counts[status] * 100 / status_total)
        status_percent[status] = percent
        assigned_percent += percent
    donut_segments = []
    donut_position = 0
    for index, (status, _label, color) in enumerate(chart_statuses):
        segment_end = (
            100
            if index == len(chart_statuses) - 1
            else donut_position + status_percent[status]
        )
        donut_segments.append(f"{color} {donut_position}% {segment_end}%")
        donut_position = segment_end
    donut_background = (
        f"conic-gradient({', '.join(donut_segments)})"
        if status_total
        else "conic-gradient(#e8eef7 0% 100%)"
    )
    available_months = set(payslips.dates("start_date", "month", order="DESC"))
    available_months.update(runs.dates("start_date", "month", order="DESC"))
    available_months.add(period_start)
    period_options = [
        {
            "key": month.strftime("%Y-%m"),
            "label": month.strftime("%B %Y"),
        }
        for month in sorted(available_months, reverse=True)
    ]
    deduction_months = set(available_months)
    month_index = period_start.year * 12 + period_start.month - 1
    for offset in range(12):
        year, month = divmod(month_index - offset, 12)
        deduction_months.add(date(year, month + 1, 1))
    deduction_period_options = [
        {"key": month.strftime("%Y-%m"), "label": month.strftime("%B %Y")}
        for month in sorted(deduction_months, reverse=True)
    ]
    month_runs = runs.filter(**period_filter)
    contracts = []
    allowances = []
    deductions = []
    reimbursements = []
    reimbursement_counts = {}
    reimbursement_filter = ""
    reimbursement_search = ""
    employees = []
    deduction_employees = []
    selected_deduction_employee = None
    selected_deduction_payroll = None
    if section == "contracts":
        contracts = Contract.objects.select_related("employee_id").order_by(
            "-contract_start_date"
        )[:100]
    elif section == "employees":
        employee_queryset = (
            Employee._base_manager
            if request.user.is_superuser
            else Employee.objects
        )
        employees = employee_queryset.select_related(
            "employee_work_info__department_id"
        ).filter(is_active=True).order_by(
            "employee_first_name", "employee_last_name"
        )[:100]
    elif section == "allowances":
        allowances = Allowance.objects.order_by("title")[:100]
    elif section == "deductions":
        deductions = Deduction.objects.order_by("title")[:100]
        employee_queryset = (
            Employee._base_manager
            if request.user.is_superuser
            else Employee.objects
        )
        deduction_employees = employee_queryset.select_related(
            "employee_work_info__department_id",
            "employee_work_info__employee_type_id",
        ).order_by("employee_first_name", "employee_last_name")
        employee_id = request.GET.get("employee", "").strip()
        if employee_id.isdigit():
            selected_deduction_employee = deduction_employees.filter(
                pk=employee_id
            ).first()
        if selected_deduction_employee is not None:
            period_start, period_end = monthly_period(period_start)
            selected_deduction_payroll = calculate_employee_payroll(
                selected_deduction_employee,
                start_date=period_start,
                end_date=period_end,
            )
    elif section == "reimbursements" and (
        request.user.is_superuser or request.user.has_perm("payroll.view_reimbursement")
    ):
        reimbursement_queryset = (
            Reimbursement._base_manager
            if request.user.is_superuser
            else Reimbursement.objects
        ).select_related("employee_id")
        reimbursement_queryset = filter_own_records(
            request,
            reimbursement_queryset,
            "payroll.view_reimbursement",
        )
        reimbursement_search = request.GET.get("search", "").strip()
        reimbursement_filter = request.GET.get("status", "")
        reimbursement_filters = {}
        if reimbursement_search:
            reimbursement_filters["title__icontains"] = reimbursement_search
        if reimbursement_filter in dict(Reimbursement.status_types):
            reimbursement_filters["status"] = reimbursement_filter
        if reimbursement_filters:
            reimbursement_queryset = reimbursement_queryset.filter(
                **reimbursement_filters
            )
        reimbursements = reimbursement_queryset.order_by("-allowance_on")[:100]
        reimbursement_counts = {
            status: (
                (
                    Reimbursement._base_manager
                    if request.user.is_superuser
                    else Reimbursement.objects
                )
                .filter(status=status)
                .count()
            )
            for status, _label in Reimbursement.status_types
        }
    context = {
        "section": section,
        "selected_period": selected_period,
        "selected_month": period_start.strftime("%B %Y"),
        "period_options": period_options,
        "deduction_period_options": deduction_period_options,
        "payslips": month_payslips.order_by("employee_id__employee_first_name")[:100],
        "runs": month_runs[:8],
        "all_runs": month_runs[:100],
        "run_count": month_runs.count(),
        "employees": employees,
        "contracts": contracts,
        "allowances": allowances,
        "deductions": deductions,
        "deduction_employees": deduction_employees,
        "selected_deduction_employee": selected_deduction_employee,
        "selected_deduction_payroll": selected_deduction_payroll,
        "reimbursements": reimbursements,
        "reimbursement_counts": reimbursement_counts,
        "reimbursement_statuses": Reimbursement.status_types,
        "reimbursement_filter": reimbursement_filter,
        "reimbursement_search": reimbursement_search,
        "status_counts": status_counts,
        "status_percent": status_percent,
        "chart_statuses": [
            {
                "label": label,
                "count": status_counts[status],
                "percent": status_percent[status],
            }
            for status, label, _color in chart_statuses
        ],
        "donut_background": donut_background,
        "employee_count": summary["employee_count"] or 0,
        "payslip_count": payslip_count,
        "total_net": summary["total_net"] or 0,
        "total_deductions": summary["total_deductions"] or 0,
        "pending_runs": month_runs.filter(status__in=("draft", "submitted")).count(),
        "is_superuser": request.user.is_superuser,
        "user_name": request.user.get_full_name()
        or (_("Super Admin") if request.user.is_superuser else _("Payroll Admin")),
        "user_role": _("Super Admin") if request.user.is_superuser else _("Payroll Admin"),
        "user_email": request.user.email,
        "perms": PermWrapper(request.user),
        "csrf_token": get_token(request),
    }
    return HttpResponse(
        render_to_string(
            "payroll/management_dashboard.html",
            context,
        )
    )


@login_required
def generate_employee_payslip(request):
    """Create the authenticated employee's current payroll snapshot."""
    if request.method != "POST":
        return redirect("payroll-dashboard")
    employee = getattr(request.user, "employee_get", None)
    if employee is None:
        return redirect("login")
    create_current_payslip(employee)
    return redirect("payroll-dashboard")


@django_login_required
@permission_required("payroll.view_contract")
def view_payroll_dashboard(request):
    """
    Dashboard rendering views
    """
    from payroll.forms.forms import DashboardExport

    selected_period = request.GET.get("period", "")
    if selected_period:
        try:
            period_start = datetime.strptime(selected_period, "%Y-%m").date()
        except ValueError:
            return HttpResponseBadRequest(_("Select a valid payroll period."))
    else:
        period_start = timezone.localdate().replace(day=1)
        selected_period = period_start.strftime("%Y-%m")
    period_filter = {
        "start_date__year": period_start.year,
        "start_date__month": period_start.month,
    }
    paid = Payslip.objects.filter(status="paid", **period_filter)
    posted = Payslip.objects.filter(status="confirmed", **period_filter)
    review_ongoing = Payslip.objects.filter(status="review_ongoing", **period_filter)
    draft = Payslip.objects.filter(status="draft", **period_filter)
    export_form = DashboardExport()
    payroll_settings = PayrollSettings.objects.first()
    context = {
        "paid": paid,
        "posted": posted,
        "review_ongoing": review_ongoing,
        "draft": draft,
        "export_form": export_form,
        "paid_net_total": paid.aggregate(total=Sum("net_pay"))["total"] or 0,
        "confirmed_net_total": posted.aggregate(total=Sum("net_pay"))["total"] or 0,
        "period_value": selected_period,
        "currency": payroll_settings.currency_symbol if payroll_settings else "₹",
        "position": payroll_settings.position if payroll_settings else "postfix",
    }
    return render(request, "payroll/dashboard.html", context=context)


@login_required
def dashboard_employee_chart(request):
    """
    payroll dashboard employee chart data
    """

    date = request.GET.get("period")
    year = date.split("-")[0]
    month = date.split("-")[1]
    dataset = []

    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if is_ajax and request.method == "GET":
        employee_list = Payslip.objects.filter(
            Q(start_date__month=month) & Q(start_date__year=year)
        )
        labels = []
        for employee in employee_list:
            labels.append(employee.employee_id)

        colors = [
            "rgba(255, 99, 132, 1)",  # Red
            "rgba(255, 206, 86, 1)",  # Yellow
            "rgba(54, 162, 235, 1)",  # Blue
            "rgba(75, 242, 182, 1)",  # green
        ]

        for choice, color in zip(Payslip.status_choices, colors):
            dataset.append(
                {
                    "label": choice[0],
                    "data": [],
                    "backgroundColor": color,
                }
            )

        employees = [employee.employee_id for employee in employee_list]

        employees = list(set(employees))
        total_pay_with_status = defaultdict(lambda: defaultdict(float))

        for label in employees:
            payslips = employee_list.filter(employee_id=label)
            for payslip in payslips:
                total_pay_with_status[payslip.status][label] += round(
                    payslip.net_pay, 2
                )

        for data in dataset:
            dataset_label = data["label"]
            data["data"] = [
                total_pay_with_status[dataset_label][label] for label in employees
            ]

        employee_label = []
        for employee in employees:
            employee_label.append(
                f"{employee.employee_first_name} {employee.employee_last_name}"
            )

        for value, choice in zip(dataset, Payslip.status_choices):
            if value["label"] == choice[0]:
                value["label"] = choice[1]

        list_of_employees = list(
            Employee.objects.values_list(
                "id", "employee_first_name", "employee_last_name"
            )
        )
        response = {
            "dataset": dataset,
            "labels": employee_label,
            "employees": list_of_employees,
            "message": _("No payslips generated for this month."),
        }
        return JsonResponse(response)


def payslip_details(request):
    """
    payroll dashboard payslip details data
    """

    date = request.GET.get("period")
    year = date.split("-")[0]
    month = date.split("-")[1]
    employee_list = []
    employee_list = Payslip.objects.filter(
        Q(start_date__month=month) & Q(start_date__year=year)
    )
    total_amount = 0
    for employee in employee_list:
        total_amount += employee.net_pay

    response = {
        "no_of_emp": len(employee_list),
        "total_amount": round(total_amount, 2),
    }
    return JsonResponse(response)


@login_required
def dashboard_department_chart(request):
    """
    payroll dashboard department chart data
    """

    date = request.GET.get("period")
    year = date.split("-")[0]
    month = date.split("-")[1]
    dataset = [
        {
            "label": "",
            "data": [],
            "backgroundColor": ["#8de5b3", "#f0a8a6", "#8ed1f7", "#f8e08e", "#c2c7cc"],
        }
    ]
    department = []
    department_total = []

    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if is_ajax and request.method == "GET":
        employee_list = Payslip.objects.filter(
            Q(start_date__month=month) & Q(start_date__year=year)
        )

        for employee in employee_list:
            department.append(
                employee.employee_id.employee_work_info.department_id.department
            )

        department = list(set(department))
        for depart in department:
            department_total.append({"department": depart, "amount": 0})

        for employee in employee_list:
            employee_department = (
                employee.employee_id.employee_work_info.department_id.department
            )

            for depart in department_total:
                if depart["department"] == employee_department:
                    depart["amount"] += round(employee.net_pay, 2)

        colors = generate_colors(len(department))

        dataset = [
            {
                "label": "",
                "data": [],
                "backgroundColor": colors,
            }
        ]

        for depart_total, depart in zip(department_total, department):
            if depart == depart_total["department"]:
                dataset[0]["data"].append(depart_total["amount"])

        response = {
            "dataset": dataset,
            "labels": department,
            "department_total": department_total,
            "message": _("No payslips generated for this month."),
        }
        return JsonResponse(response)


def contract_ending(request):
    """
    payroll dashboard contract ending details data
    """

    date = request.GET.get("period")
    month = date.split("-")[1]
    year = date.split("-")[0]

    if request.GET.get("initialLoad") == "true":
        if month == "12":
            month = 0
            year = int(year) + 1

        contract_end = Contract.objects.filter(
            contract_end_date__month=int(month) + 1, contract_end_date__year=int(year)
        )
    else:
        contract_end = Contract.objects.filter(
            contract_end_date__month=int(month), contract_end_date__year=int(year)
        )

    ending_contract = []
    for contract in contract_end:
        ending_contract.append(
            {"contract_name": contract.contract_name, "contract_id": contract.id}
        )

    response = {
        "contract_end": ending_contract,
        "message": _("No contracts ending this month"),
    }
    return JsonResponse(response)


def payslip_export(request):
    """
    payroll dashboard exporting to excell data

    Args:
    - request (HttpRequest): The HTTP request object.
    - contract_id (int): The ID of the contract to view.

    """

    start_date = request.POST.get("start_date")
    end_date = request.POST.get("end_date")
    employee = request.POST.getlist("employees")
    status = request.POST.get("status")
    contributions = (
        request.POST.getlist("contributions")
        if request.POST.getlist("contributions")
        else get_active_employees(None)["get_active_employees"].values_list(
            "id", flat=True
        )
    )
    department = []
    total_amount = 0

    table1_data = []
    table2_data = []
    table3_data = []
    table4_data = []
    table5_data = []

    employee_payslip_list = Payslip.objects.all()

    if start_date:
        employee_payslip_list = employee_payslip_list.filter(start_date__gte=start_date)

    if end_date:
        employee_payslip_list = employee_payslip_list.filter(end_date__lte=end_date)

    if employee:
        employee_payslip_list = employee_payslip_list.filter(employee_id__in=employee)

    if status:
        employee_payslip_list = employee_payslip_list.filter(status=status)

    for employ in contributions:
        payslips = Payslip.objects.filter(employee_id__id=employ)
        if end_date:
            payslips = Payslip.objects.filter(
                employee_id__id=employ, end_date__lte=end_date
            )
        if start_date:
            payslips = Payslip.objects.filter(
                employee_id__id=employ, start_date__gte=start_date
            )
            if end_date:
                payslips = payslips.filter(end_date__lte=end_date)
        pay_heads = payslips.values_list("pay_head_data", flat=True)
        # contribution_deductions = []
        deductions = []
        for head in pay_heads:
            for deduction in head["gross_pay_deductions"]:
                if deduction.get("deduction_id"):
                    deductions.append(deduction)
            for deduction in head["basic_pay_deductions"]:
                if deduction.get("deduction_id"):
                    deductions.append(deduction)
            for deduction in head["pretax_deductions"]:
                if deduction.get("deduction_id"):
                    deductions.append(deduction)
            for deduction in head["post_tax_deductions"]:
                if deduction.get("deduction_id"):
                    deductions.append(deduction)
            for deduction in head["tax_deductions"]:
                if deduction.get("deduction_id"):
                    deductions.append(deduction)
            for deduction in head["net_deductions"]:
                deductions.append(deduction)

        deductions.sort(key=lambda x: x["deduction_id"])
        grouped_deductions = {
            key: list(group)
            for key, group in groupby(deductions, key=lambda x: x["deduction_id"])
        }

        for deduction_id, group in grouped_deductions.items():
            employee_contribution = sum(item["amount"] for item in group)
            try:
                employer_contribution = sum(
                    item["employer_contribution_amount"] for item in group
                )
            except:
                employer_contribution = 0
            if employer_contribution > 0:
                table5_data.append(
                    {
                        "Employee": Employee.objects.get(id=employ),
                        "Employer Contribution": employer_contribution,
                        "Employee Contribution": employee_contribution,
                    }
                )

    emp = request.user.employee_get
    if employee_payslip_list:
        for payslip in employee_payslip_list:
            # Taking the company_name of the user
            info = EmployeeWorkInformation.objects.filter(employee_id=emp).first()

            if info:
                employee_company = info.company_id
                company_name = Company.objects.filter(company=employee_company).first()
                date_format = (
                    company_name.date_format
                    if company_name and company_name.date_format
                    else "MMM. D, YYYY"
                )
            else:
                date_format = "MMM. D, YYYY"

            start_date_str = str(payslip.start_date)
            end_date_str = str(payslip.end_date)

            # Convert the string to a datetime.date object
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
            end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()

            for format_name, format_string in HORILLA_DATE_FORMATS.items():
                if format_name == date_format:
                    formatted_start_date = start_date.strftime(format_string)

            for format_name, format_string in HORILLA_DATE_FORMATS.items():
                if format_name == date_format:
                    formatted_end_date = end_date.strftime(format_string)

            table1_data.append(
                {
                    "employee": f"{payslip.employee_id.employee_first_name} {payslip.employee_id.employee_last_name}",
                    "start_date": formatted_start_date,
                    "end_date": formatted_end_date,
                    "basic_pay": round(payslip.basic_pay, 2),
                    "deduction": round(payslip.deduction, 2),
                    "allowance": round(payslip.gross_pay - payslip.basic_pay, 2),
                    "gross_pay": round(payslip.gross_pay, 2),
                    "net_pay": round(payslip.net_pay, 2),
                    "status": status_choices.get(payslip.status),
                },
            )
    else:
        table1_data.append(
            {
                "employee": "None",
                "start_date": "None",
                "end_date": "None",
                "basic_pay": "None",
                "deduction": "None",
                "allowance": "None",
                "gross_pay": "None",
                "net_pay": "None",
                "status": "None",
            },
        )

    for employee in employee_payslip_list:
        department.append(
            employee.employee_id.employee_work_info.department_id.department
        )

    department = list(set(department))

    for depart in department:
        table2_data.append({"Department": depart, "Amount": 0})

    for employee in employee_payslip_list:
        employee_department = (
            employee.employee_id.employee_work_info.department_id.department
        )

        for depart in table2_data:
            if depart["Department"] == employee_department:
                depart["Amount"] += round(employee.net_pay, 2)

    if not employee_payslip_list:
        table2_data.append({"Department": "None", "Amount": 0})

    contract_end = Contract.objects.all()
    if not start_date and not end_date:
        contract_end = contract_end.filter(
            Q(contract_end_date__month=datetime.now().month)
            & Q(contract_end_date__year=datetime.now().year)
        )
    if end_date:
        contract_end = contract_end.filter(contract_end_date__lte=end_date)

    if start_date:
        if not end_date:
            contract_end = contract_end.filter(
                Q(contract_end_date__gte=start_date)
                & Q(contract_end_date__lte=datetime.now())
            )
        else:
            contract_end = contract_end.filter(contract_end_date__gte=start_date)

    table3_data = {"contract_ending": []}

    for contract in contract_end:
        table3_data["contract_ending"].append(contract.contract_name)

    if not contract_end:
        table3_data["contract_ending"].append("None")

    for employee in employee_payslip_list:
        total_amount += round(employee.net_pay, 2)

    table4_data = {
        "no_of_payslip_generated": len(employee_payslip_list),
        "total_amount": [total_amount],
    }

    df_table1 = pd.DataFrame(table1_data)
    df_table2 = pd.DataFrame(table2_data)
    df_table3 = pd.DataFrame(table3_data)
    df_table4 = pd.DataFrame(table4_data)
    df_table5 = pd.DataFrame(table5_data)

    df_table1 = df_table1.rename(
        columns={
            "employee": "Employee",
            "start_date": "Start Date",
            "end_date": "End Date",
            "deduction": "Deduction",
            "allowance": "Allowance",
            "gross_pay": "Gross Pay",
            "net_pay": "Net Pay",
            "status": "Status",
        }
    )

    df_table3 = df_table3.rename(
        columns={
            "contract_ending": (
                f"Contract Ending {start_date} to {end_date}"
                if start_date and end_date
                else f"Contract Ending"
            ),
        }
    )

    df_table4 = df_table4.rename(
        columns={
            "no_of_payslip_generated": "Number of payslips generated",
            "total_amount": "Total Amount",
        }
    )

    df_table5 = df_table5.rename(
        columns={
            "contract_ending": (
                f"Employee - Employer Contributions {start_date} to {end_date}"
                if start_date and end_date
                else f"Contract Ending"
            ),
        }
    )

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = "attachment; filename=payslip.xlsx"

    writer = pd.ExcelWriter(response, engine="xlsxwriter")
    df_table1.to_excel(
        writer, sheet_name="Payroll Dashboard details", index=False, startrow=3
    )
    df_table2.to_excel(
        writer,
        sheet_name="Payroll Dashboard details",
        index=False,
        startrow=len(df_table1) + 3 + 3,
    )
    df_table3.to_excel(
        writer,
        sheet_name="Payroll Dashboard details",
        index=False,
        startrow=len(df_table1) + 3 + len(df_table2) + 6,
    )
    df_table5.to_excel(
        writer,
        sheet_name="Payroll Dashboard details",
        index=False,
        startrow=len(df_table1) + 3 + len(df_table2) + len(df_table3) + 9,
    )
    df_table4.to_excel(
        writer,
        sheet_name="Payroll Dashboard details",
        index=False,
        startrow=len(df_table1)
        + 3
        + len(df_table2)
        + len(df_table3)
        + len(df_table5)
        + 12,
    )

    workbook = writer.book
    worksheet = writer.sheets["Payroll Dashboard details"]
    max_columns = max(
        len(df_table1.columns),
        len(df_table2.columns),
        len(df_table3.columns),
        len(df_table4.columns),
        len(df_table5.columns),
    )

    heading_format = workbook.add_format(
        {
            "bold": True,
            "font_size": 14,
            "align": "center",
            "valign": "vcenter",
            "bg_color": "#eb7968",
            "font_size": 20,
        }
    )

    worksheet.set_row(0, 30)
    worksheet.merge_range(
        0,
        0,
        0,
        max_columns - 1,
        (
            f"Payroll details {start_date} to {end_date}"
            if start_date and end_date
            else f"Payroll details"
        ),
        heading_format,
    )

    header_format = workbook.add_format(
        {"bg_color": "#eb7968", "bold": True, "text_wrap": True}
    )

    for col_num, value in enumerate(df_table1.columns.values):
        worksheet.write(3, col_num, value, header_format)
        col_letter = chr(65 + col_num)

        header_width = max(len(value) + 2, len(df_table1[value].astype(str).max()) + 2)
        worksheet.set_column(f"{col_letter}:{col_letter}", header_width)

    for col_num, value in enumerate(df_table2.columns.values):
        worksheet.write(len(df_table1) + 3 + 3, col_num, value, header_format)
        col_letter = chr(65 + col_num)

        header_width = max(len(value) + 2, len(df_table2[value].astype(str).max()) + 2)
        worksheet.set_column(f"{col_letter}:{col_letter}", header_width)

    for col_num, value in enumerate(df_table3.columns.values):
        worksheet.write(
            len(df_table1) + 3 + len(df_table2) + 6, col_num, value, header_format
        )
        col_letter = chr(65 + col_num)

        header_width = max(len(value) + 2, len(df_table3[value].astype(str).max()) + 2)
        worksheet.set_column(f"{col_letter}:{col_letter}", header_width)

    for col_num, value in enumerate(df_table5.columns.values):
        worksheet.write(
            len(df_table1) + 3 + len(df_table2) + len(df_table3) + 9,
            col_num,
            value,
            header_format,
        )
        col_letter = chr(65 + col_num)

    for col_num, value in enumerate(df_table4.columns.values):
        worksheet.write(
            len(df_table1) + 3 + len(df_table2) + len(df_table3) + len(df_table5) + 12,
            col_num,
            value,
            header_format,
        )
        col_letter = chr(65 + col_num)

        header_width = max(len(value) + 2, len(df_table4[value].astype(str).max()) + 2)
        worksheet.set_column(f"{col_letter}:{col_letter}", header_width)

    worksheet.set_row(len(df_table1) + len(df_table2) + 9, 30)

    writer.close()

    return response


@login_required
@permission_required("payroll.delete_payslip")
def payslip_bulk_delete(request):
    """
    This method is used to bulk delete for Payslip
    """
    ids = request.POST["ids"]
    ids = json.loads(ids)
    for id in ids:
        try:
            payslip = Payslip.objects.get(id=id)
            period = f"{payslip.start_date} to {payslip.end_date}"
            payslip.delete()
            messages.success(
                request,
                _("{employee} {period} payslip deleted.").format(
                    employee=payslip.employee_id, period=period
                ),
            )
        except Payslip.DoesNotExist:
            messages.error(request, _("Payslip not found."))
        except ProtectedError:
            messages.error(
                request,
                _("You cannot delete {payslip}").format(payslip=payslip),
            )
    return JsonResponse({"message": "Success"})


@login_required
@permission_required("payroll.change_payslip")
def slip_group_name_update(request):
    """
    This method is used to update the group of the payslip
    """
    new_name = request.POST["newName"]
    group_name = request.POST["previousName"]
    Payslip.objects.filter(group_name=group_name).update(group_name=new_name)
    return JsonResponse(
        {"type": "success", "message": "Batch name updated.", "new_name": new_name}
    )


@login_required
@permission_required("payroll.add_contract")
def contract_export(request):
    hx_request = request.META.get("HTTP_HX_REQUEST")
    if hx_request:
        export_filter = ContractFilter()
        export_column = ContractExportFieldForm()
        content = {
            "export_filter": export_filter,
            "export_column": export_column,
        }
        return render(
            request,
            "payroll/contract/contract_export_filter.html",
            context=content,
        )
    return export_data(
        request=request,
        model=Contract,
        filter_class=ContractFilter,
        form_class=ContractExportFieldForm,
        file_name="Contract_export",
    )


@login_required
@permission_required("payroll.delete_contract")
def contract_bulk_delete(request):
    """
    This method is used to bulk delete Contract
    """
    ids = request.POST["ids"]
    ids = json.loads(ids)
    for id in ids:
        try:
            contract = Contract.objects.get(id=id)
            name = f"{contract.contract_name}"
            contract.delete()
            messages.success(
                request,
                _("{name} deleted.").format(name=name),
            )
        except Payslip.DoesNotExist:
            messages.error(request, _("Contract not found."))
        except ProtectedError:
            messages.error(
                request,
                _("You cannot delete {contract}").format(contract=contract),
            )
    return JsonResponse({"message": "Success"})


def equalize_lists_length(allowances, deductions):
    """
    Equalize the lengths of two lists by appending empty dictionaries to the shorter list.

    Args:
    deductions (list): List of dictionaries representing deductions.
    allowances (list): List of dictionaries representing allowances.

    Returns:
    tuple: Tuple containing two lists with equal lengths.
    """
    num_deductions = len(deductions)
    num_allowances = len(allowances)

    while num_deductions < num_allowances:
        deductions.append({"title": "", "amount": ""})
        num_deductions += 1

    while num_allowances < num_deductions:
        allowances.append({"title": "", "amount": ""})
        num_allowances += 1

    return deductions, allowances


def generate_payslip_pdf(template_path, context, html=False):
    """
    Generate a PDF file from an HTML template and context data.

    Args:
        template_path (str): The path to the HTML template.
        context (dict): The context data to render the template.
        html (bool): If True, return raw HTML instead of a PDF.

    Returns:
        HttpResponse: A response with the generated PDF file or raw HTML.
    """
    try:
        # Render the HTML content from the template and context
        html_content = render_to_string(template_path, context)

        # Return raw HTML if requested
        if html:
            return HttpResponse(html_content, content_type="text/html")

        # PDF options for pdfkit
        pdf_options = {
            "page-size": "A4",
            "margin-top": "10mm",
            "margin-bottom": "10mm",
            "margin-left": "10mm",
            "margin-right": "10mm",
            "encoding": "UTF-8",
            "enable-local-file-access": None,  # Required to load local CSS/images
            "dpi": 300,
            "zoom": 1.3,
            "footer-center": "[page]/[topage]",  # Required to load local CSS/images
        }

        # Generate the PDF as binary content
        pdf = pdfkit.from_string(html_content, False, options=pdf_options)

        # Return an HttpResponse containing the PDF content
        response = HttpResponse(pdf, content_type="application/pdf")
        response["Content-Disposition"] = "inline; filename=payslip.pdf"
        return response
    except Exception as e:
        # Handle errors gracefully
        return HttpResponse(f"Error generating PDF: {str(e)}", status=500)


def payslip_pdf(request, id):
    """
    Generate the payslip as a PDF and return it in an HttpResponse.

    Args:
        request (HttpRequest): The request object.
        id (int): The ID of the payslip to generate.

    Returns:
        HttpResponse: A response containing the PDF content.
    """

    from .component_views import filter_payslip

    if Payslip.objects.filter(id=id).exists():
        payslip = Payslip.objects.get(id=id)
        company = Company.objects.filter(hq=True).first()
        if (
            request.user.has_perm("payroll.view_payslip")
            or payslip.employee_id.employee_user_id == request.user
        ):
            user = request.user
            employee = getattr(user, "employee_get", None) or payslip.employee_id

            # Taking the company_name of the user
            info = EmployeeWorkInformation.objects.filter(employee_id=employee)
            date_format = "MMM. D, YYYY"
            if info.exists():
                for data in info:
                    employee_company = data.company_id
                company_name = Company.objects.filter(company=employee_company)
                emp_company = company_name.first()

                # Access the date_format attribute directly
                date_format = (
                    emp_company.date_format
                    if emp_company and emp_company.date_format
                    else "MMM. D, YYYY"
                )

            data = payslip.pay_head_data.copy()
            start_date_str = data["start_date"]
            end_date_str = data["end_date"]

            # Convert the string to a datetime.date object
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
            end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()

            # Format the start and end dates
            for format_name, format_string in HORILLA_DATE_FORMATS.items():
                if format_name == date_format:
                    formatted_start_date = start_date.strftime(format_string)
                    formatted_end_date = end_date.strftime(format_string)

            # Prepare context for the template
            data.update(
                {
                    "month_start_name": start_date.strftime("%B %d, %Y"),
                    "month_end_name": end_date.strftime("%B %d, %Y"),
                    "formatted_start_date": formatted_start_date,
                    "formatted_end_date": formatted_end_date,
                    "employee": payslip.employee_id,
                    "payslip": payslip,
                    "json_data": data.copy(),
                    "currency": (
                        PayrollSettings.objects.first().currency_symbol
                        if PayrollSettings.objects.exists()
                        else "₹"
                    ),
                    "all_deductions": [],
                    "all_allowances": data["allowances"].copy(),
                    "host": request.get_host(),
                    "protocol": "https" if request.is_secure() else "http",
                    "company": company,
                }
            )

            # Merge deductions and allowances for display
            for deduction_list in [
                data["basic_pay_deductions"],
                data["gross_pay_deductions"],
                data["pretax_deductions"],
                data["post_tax_deductions"],
                data["tax_deductions"],
                data["net_deductions"],
            ]:
                data["all_deductions"].extend(deduction_list)

            equalize_lists_length(data["allowances"], data["all_deductions"])
            data["zipped_data"] = zip(data["allowances"], data["all_deductions"])
            template_path = "payroll/payslip/payslip_pdf.html"

            return generate_payslip_pdf(template_path, context=data, html=False)
        return redirect(filter_payslip)
    return render(request, "405.html")


@login_required
@permission_required("payroll.view_contract")
def contract_select(request):
    page_number = request.GET.get("page")

    if page_number == "all":
        employees = Contract.objects.all()

    contract_ids = [str(emp.id) for emp in employees]
    total_count = employees.count()

    context = {"contract_ids": contract_ids, "total_count": total_count}

    return JsonResponse(context, safe=False)


@login_required
def contract_select_filter(request):
    page_number = request.GET.get("page")
    filtered = request.GET.get("filter")
    filters = json.loads(filtered) if filtered else {}

    if page_number == "all":
        contract_filter = ContractFilter(filters, queryset=Contract.objects.all())

        # Get the filtered queryset
        filtered_employees = contract_filter.qs

        contract_ids = [str(emp.id) for emp in filtered_employees]
        total_count = filtered_employees.count()

        context = {"contract_ids": contract_ids, "total_count": total_count}

        return JsonResponse(context)


@login_required
def payslip_select(request):
    page_number = request.GET.get("page")

    if page_number == "all":
        if request.user.has_perm("payroll.view_payslip"):
            employees = Payslip.objects.all()
        else:
            employees = Payslip.objects.filter(
                employee_id__employee_user_id=request.user
            )

    payslip_ids = [str(emp.id) for emp in employees]
    total_count = employees.count()

    context = {"payslip_ids": payslip_ids, "total_count": total_count}

    return JsonResponse(context, safe=False)


@login_required
def payslip_select_filter(request):
    page_number = request.GET.get("page")
    filtered = request.GET.get("filter")
    filters = json.loads(filtered) if filtered else {}

    if page_number == "all":
        payslip_filter = PayslipFilter(filters, queryset=Payslip.objects.all())

        # Get the filtered queryset
        filtered_employees = payslip_filter.qs

        payslip_ids = [str(emp.id) for emp in filtered_employees]
        total_count = filtered_employees.count()

        context = {"payslip_ids": payslip_ids, "total_count": total_count}

        return JsonResponse(context)


@login_required
def create_payrollrequest_comment(request, payroll_id):
    """
    This method renders form and template to create Reimbursement request comments
    """
    from payroll.forms.forms import ReimbursementRequestCommentForm

    payroll = Reimbursement.objects.filter(id=payroll_id).first()
    emp = request.user.employee_get
    form = ReimbursementRequestCommentForm(
        initial={"employee_id": emp.id, "request_id": payroll_id}
    )

    if request.method == "POST":
        form = ReimbursementRequestCommentForm(request.POST)
        if form.is_valid():
            form.instance.employee_id = emp
            form.instance.request_id = payroll
            form.save()
            comments = ReimbursementrequestComment.objects.filter(
                request_id=payroll_id
            ).order_by("-created_at")
            no_comments = False
            if not comments.exists():
                no_comments = True
            form = ReimbursementRequestCommentForm(
                initial={"employee_id": emp.id, "request_id": payroll_id}
            )
            messages.success(request, _("Comment added successfully!"))

            if payroll.employee_id.employee_work_info.reporting_manager_id is not None:

                if request.user.employee_get.id == payroll.employee_id.id:
                    rec = (
                        payroll.employee_id.employee_work_info.reporting_manager_id.employee_user_id
                    )
                    send_payroll_notification(
                        request.user.employee_get,
                        recipient=rec,
                        verb=f"{payroll.employee_id}'s reimbursement request has received a comment.",
                        verb_ar=f"تلقى طلب استرداد نفقات {payroll.employee_id} تعليقًا.",
                        verb_de=f"{payroll.employee_id}s Rückerstattungsantrag hat einen Kommentar erhalten.",
                        verb_es=f"La solicitud de reembolso de gastos de {payroll.employee_id} ha recibido un comentario.",
                        verb_fr=f"La demande de remboursement de frais de {payroll.employee_id} a reçu un commentaire.",
                        redirect=reverse("view-reimbursement"),
                        icon="chatbox-ellipses",
                    )
                elif (
                    request.user.employee_get.id
                    == payroll.employee_id.employee_work_info.reporting_manager_id.id
                ):
                    rec = payroll.employee_id.employee_user_id
                    send_payroll_notification(
                        request.user.employee_get,
                        recipient=rec,
                        verb="Your reimbursement request has received a comment.",
                        verb_ar="تلقى طلب استرداد نفقاتك تعليقًا.",
                        verb_de="Ihr Rückerstattungsantrag hat einen Kommentar erhalten.",
                        verb_es="Tu solicitud de reembolso ha recibido un comentario.",
                        verb_fr="Votre demande de remboursement a reçu un commentaire.",
                        redirect=reverse("view-reimbursement"),
                        icon="chatbox-ellipses",
                    )
                else:
                    rec = [
                        payroll.employee_id.employee_user_id,
                        payroll.employee_id.employee_work_info.reporting_manager_id.employee_user_id,
                    ]
                    send_payroll_notification(
                        request.user.employee_get,
                        recipient=rec,
                        verb=f"{payroll.employee_id}'s reimbursement request has received a comment.",
                        verb_ar=f"تلقى طلب استرداد نفقات {payroll.employee_id} تعليقًا.",
                        verb_de=f"{payroll.employee_id}s Rückerstattungsantrag hat einen Kommentar erhalten.",
                        verb_es=f"La solicitud de reembolso de gastos de {payroll.employee_id} ha recibido un comentario.",
                        verb_fr=f"La demande de remboursement de frais de {payroll.employee_id} a reçu un commentaire.",
                        redirect=reverse("view-reimbursement"),
                        icon="chatbox-ellipses",
                    )
            else:
                rec = payroll.employee_id.employee_user_id
                send_payroll_notification(
                    request.user.employee_get,
                    recipient=rec,
                    verb="Your reimbursement request has received a comment.",
                    verb_ar="تلقى طلب استرداد نفقاتك تعليقًا.",
                    verb_de="Ihr Rückerstattungsantrag hat einen Kommentar erhalten.",
                    verb_es="Tu solicitud de reembolso ha recibido un comentario.",
                    verb_fr="Votre demande de remboursement a reçu un commentaire.",
                    redirect=reverse("view-reimbursement"),
                    icon="chatbox-ellipses",
                )
            notify_payroll_admins(
                request.user,
                payroll.employee_id.employee_work_info.company_id,
                f"{payroll.employee_id}'s reimbursement request received a comment.",
                redirect=reverse("view-reimbursement") + f"?id={payroll_id}",
                icon="chatbox-ellipses",
            )

            return render(
                request,
                "payroll/reimbursement/reimbursement_comment.html",
                {
                    "comments": comments,
                    "no_comments": no_comments,
                    "request_id": payroll_id,
                },
            )
    return render(
        request,
        "payroll/reimbursement/reimbursement_comment.html",
        {"form": form, "request_id": payroll_id},
    )


@login_required
@hx_request_required
def view_payrollrequest_comment(request, payroll_id):
    """
    This method is used to show Reimbursement request comments
    """
    comments = ReimbursementrequestComment.objects.filter(
        request_id=payroll_id
    ).order_by("-created_at")

    req = Reimbursement.objects.get(id=payroll_id)
    no_comments = False
    if not comments.exists():
        no_comments = True

    if request.FILES:
        files = request.FILES.getlist("files")
        comment_id = request.GET["comment_id"]
        comment = ReimbursementrequestComment.objects.get(id=comment_id)
        attachments = []
        for file in files:
            file_instance = ReimbursementFile()
            file_instance.file = file
            file_instance.save()
            attachments.append(file_instance)
        comment.files.add(*attachments)
    return render(
        request,
        "payroll/reimbursement/reimbursement_comment.html",
        {
            "comments": comments,
            "no_comments": no_comments,
            "request_id": payroll_id,
            "req": req,
        },
    )


@login_required
def delete_payrollrequest_comment(request, comment_id):
    """
    This method is used to delete Reimbursement request comments
    """
    script = ""
    comment = ReimbursementrequestComment.objects.filter(id=comment_id)
    comment.delete()
    messages.success(request, _("Comment deleted successfully!"))
    return HttpResponse(script)


@login_required
def delete_reimbursement_comment_file(request):
    """
    Used to delete attachment
    """
    script = ""
    ids = request.GET.getlist("ids")
    records = ReimbursementFile.objects.filter(id__in=ids)
    if not request.user.has_perm("payroll.delete_reimbursmentfile"):
        records = records.filter(employee_id__employee_user_id=request.user)
    records.delete()
    messages.success(request, _("File deleted successfully"))
    return HttpResponse(script)


@login_required
@permission_required("payroll.add_payrollgeneralsetting")
def initial_notice_period(request):
    """
    This method is used to set initial value notice period
    """
    notice_period = eval_validate(request.GET["notice_period"])
    settings = PayrollGeneralSetting.objects.first()
    settings = settings if settings else PayrollGeneralSetting()
    settings.notice_period = max(notice_period, 0)
    settings.save()
    messages.success(
        request, _("The initial notice period has been successfully updated.")
    )
    if request.META.get("HTTP_HX_REQUEST"):
        return HttpResponse()
    return HorillaRedirect(request)


# ===========================Auto payslip generate================================


@login_required
@permission_required("payroll.view_PayslipAutoGenerate")
def auto_payslip_settings_view(request):
    payslip_auto_generate = PayslipAutoGenerate.objects.all()

    context = {"payslip_auto_generate": payslip_auto_generate}
    return render(request, "payroll/settings/auto_payslip_settings.html", context)


@login_required
@hx_request_required
@permission_required("payroll.change_PayslipAutoGenerate")
def create_or_update_auto_payslip(request, auto_id=None):
    auto_payslip = None
    if auto_id:
        auto_payslip = PayslipAutoGenerate.objects.get(id=auto_id)
    form = PayslipAutoGenerateForm(instance=auto_payslip)
    if request.method == "POST":
        form = PayslipAutoGenerateForm(request.POST, instance=auto_payslip)
        if form.is_valid():
            auto_payslip = form.save()
            company = (
                auto_payslip.company_id if auto_payslip.company_id else "All company"
            )
            messages.success(
                request, _(f"Payslip Auto generate for {company} created successfully ")
            )
            return HorillaRedirect(request)
    return render(
        request, "payroll/settings/auto_payslip_create_or_update.html", {"form": form}
    )


@login_required
@permission_required("payroll.change_PayslipAutoGenerate")
def activate_auto_payslip_generate(request):
    """
    ajax function to update is active field in PayslipAutoGenerate.
    Args:
    - isChecked: Boolean value representing the state of PayslipAutoGenerate,
    - autoId: Id of PayslipAutoGenerate object
    """
    isChecked = request.POST.get("isChecked")
    autoId = request.POST.get("autoId")
    payslip_auto = PayslipAutoGenerate.objects.get(id=autoId)
    if isChecked == "true":
        payslip_auto.auto_generate = True
        response = {
            "type": "success",
            "message": _("Auto paslip generate activated successfully."),
        }
    else:
        payslip_auto.auto_generate = False
        response = {
            "type": "success",
            "message": _("Auto paslip generate deactivated successfully."),
        }
    payslip_auto.save()
    return JsonResponse(response)


@login_required
@hx_request_required
@permission_required("payroll.delete_PayslipAutoGenerate")
def delete_auto_payslip(request, auto_id):
    """
    Delete a PayslipAutoGenerate object.

    Args:
        auto_id: The ID of PayslipAutoGenerate object to delete.

    Returns:
        Redirects to the contract view after successfully deleting the contract.

    """
    try:
        auto_payslip = PayslipAutoGenerate.objects.get(id=auto_id)
        if not auto_payslip.auto_generate:
            company = (
                auto_payslip.company_id if auto_payslip.company_id else "All company"
            )
            auto_payslip.delete()
            messages.success(
                request, _(f"Payslip auto generate for {company} deleted successfully.")
            )
        else:
            messages.info(request, _(f"Active 'Payslip auto generate' cannot delete."))
        return HorillaRedirect(request)
    except PayslipAutoGenerate.DoesNotExist:
        messages.error(request, _("Payslip auto generate not found."))
    return HorillaRedirect(request)
