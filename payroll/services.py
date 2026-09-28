"""Employee-facing payroll calculations built on existing HR records."""

from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.apps import apps
from django.db.models import Q
from django.utils import timezone

from base.methods import get_company_leave_dates, get_holiday_dates
from payroll.models.models import Payslip


MONEY = Decimal("0.01")
LWP_NAMES = {"lwp", "leave without pay", "loss of pay"}
PAID_NAMES = {"cl", "casual leave", "sl", "sick leave", "al", "annual leave"}


def monthly_period(period_date=None):
    """Return the calendar-month payroll period containing ``period_date``."""
    period_date = period_date or timezone.localdate()
    return (
        date(period_date.year, period_date.month, 1),
        date(period_date.year, period_date.month, monthrange(period_date.year, period_date.month)[1]),
    )


def _dates(start_date, end_date):
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


def _working_dates(start_date, end_date):
    excluded = set(get_holiday_dates(start_date, end_date))
    excluded.update(get_company_leave_dates(start_date.year))
    if end_date.year != start_date.year:
        excluded.update(get_company_leave_dates(end_date.year))
    return [
        current
        for current in _dates(start_date, end_date)
        if current.weekday() < 5 and current not in excluded
    ]


def _employee_category(employee):
    employee_type = getattr(
        getattr(employee, "employee_work_info", None), "employee_type_id", None
    )
    return str(getattr(employee_type, "employee_type", employee_type or "")).strip().lower()


def _leave_dates(leave):
    end_date = leave.end_date or leave.start_date
    return set(_dates(leave.start_date, end_date))


def _eligible_paid_leave(leave, category):
    name = str(getattr(leave.leave_type_id, "name", "")).strip().lower()
    is_ccl = name in {"ccl", "compensatory casual leave"} or getattr(
        leave.leave_type_id, "is_compensatory_leave", False
    )
    if name in LWP_NAMES or getattr(leave.leave_type_id, "payment", "unpaid") != "paid":
        return False
    if is_ccl:
        compensatory_model = apps.get_model("leave", "CompensatoryLeaveRequest")
        earned_on = compensatory_model.objects.filter(
            employee_id=leave.employee_id,
            leave_type_id=leave.leave_type_id,
            status="approved",
            requested_date__lte=leave.start_date,
            requested_date__gte=leave.start_date - timedelta(days=90),
        ).filter(
            attendance_id__attendance_validated=True,
        ).filter(
            Q(attendance_id__is_holiday=True)
            | Q(attendance_id__attendance_date__week_day__in=[1, 7])
        ).exists()
        if not earned_on:
            return False
    if is_ccl:
        return True
    if "probation" in category or "intern" in category:
        return False
    if "contract" in category:
        return False
    return name in PAID_NAMES or name in {"ccl", "compensatory casual leave"}


def calculate_employee_payroll(employee, start_date=None, end_date=None):
    """Calculate one deterministic monthly payroll result for an employee.

    Existing contract configuration controls the daily salary divisor. If no
    active contract exists, the project's monthly-working-day convention is
    used. No tax, PF, ESI, overtime, bonus, or other unspecified rule is
    invented here.
    """
    start_date, end_date = (
        monthly_period() if start_date is None or end_date is None else (start_date, end_date)
    )
    calculation_end = min(end_date, timezone.localdate())
    work_info = getattr(employee, "employee_work_info", None)
    salary = Decimal(str(getattr(work_info, "basic_salary", 0) or 0))
    working_dates = _working_dates(start_date, calculation_end)
    working_set = set(working_dates)
    working_days = len(working_dates)
    daily_rate = (salary / Decimal(working_days)) if working_days else Decimal("0")

    contract = employee.contract_set.filter(
        is_active=True, contract_status="active"
    ).first()
    if contract and not contract.calculate_daily_leave_amount:
        daily_rate = Decimal(str(contract.deduction_for_one_leave_amount or 0))

    category = _employee_category(employee)
    approved_paid = set()
    approved_unpaid = set()
    approved_lwp = set()
    pending_or_unapproved = set()
    lwp_dates_year = set()
    year_start = date(start_date.year, 1, 1)
    year_end = date(start_date.year, 12, 31)
    leave_queryset = employee.leaverequest_set.select_related("leave_type_id").filter(
        start_date__lte=end_date,
        end_date__gte=start_date,
    )
    yearly_lwp_queryset = employee.leaverequest_set.select_related("leave_type_id").filter(
        start_date__lte=year_end,
        end_date__gte=year_start,
        status="approved",
        leave_type_id__name__iregex=r"^(lwp|leave without pay|loss of pay)$",
    )
    for leave in yearly_lwp_queryset:
        lwp_dates_year.update(
            d for d in _leave_dates(leave) if year_start <= d <= year_end
        )
    for leave in leave_queryset:
        dates = _leave_dates(leave)
        name = str(getattr(leave.leave_type_id, "name", "")).strip().lower()
        if getattr(leave.leave_type_id, "leave_unit", "day") == "minute":
            continue
        if leave.status == "approved" and _eligible_paid_leave(leave, category):
            approved_paid.update(dates)
        elif leave.status == "approved":
            approved_unpaid.update(dates)
            if name in LWP_NAMES:
                approved_lwp.update(dates)
        else:
            pending_or_unapproved.update(dates)

    attendance_dates = set()
    if apps.is_installed("attendance"):
        Attendance = apps.get_model("attendance", "Attendance")
        attendance_dates.update(
            Attendance.objects.filter(
                employee_id=employee,
                attendance_date__range=(start_date, end_date),
                attendance_validated=True,
            ).values_list("attendance_date", flat=True)
        )

    sandwich_dates = set()
    all_leave_dates = approved_paid | approved_unpaid | pending_or_unapproved
    for current in _dates(start_date, calculation_end):
        if current.weekday() == 6 and (current - timedelta(days=1)) in all_leave_dates and (
            current + timedelta(days=1)
        ) in all_leave_dates:
            sandwich_dates.add(current)

    unpaid_dates = (approved_unpaid | pending_or_unapproved) & working_set
    paid_dates = (approved_paid & working_set) - unpaid_dates
    absence_dates = (working_set - attendance_dates - approved_paid - approved_unpaid - pending_or_unapproved)
    unpaid_dates.update(absence_dates)

    lwp_period_dates = approved_lwp & working_set
    excess_lwp_dates = set(sorted(lwp_dates_year)[12:])
    double_lwp_dates = lwp_period_dates & excess_lwp_dates
    normal_lwp_dates = lwp_period_dates - double_lwp_dates
    other_unpaid_dates = (
        (approved_unpaid - approved_lwp) | pending_or_unapproved
    ) & working_set
    absence_only_dates = absence_dates - lwp_period_dates - other_unpaid_dates
    absence_deduction = daily_rate * len(absence_only_dates)
    other_unpaid_deduction = daily_rate * len(other_unpaid_dates)
    lwp_deduction = daily_rate * len(normal_lwp_dates)
    double_lwp_deduction = daily_rate * 2 * len(double_lwp_dates)
    sandwich_deduction = daily_rate * len(sandwich_dates)
    total_deductions = (
        absence_deduction
        + other_unpaid_deduction
        + lwp_deduction
        + double_lwp_deduction
        + sandwich_deduction
    )
    net_pay = salary - total_deductions

    def money(value):
        return value.quantize(MONEY, rounding=ROUND_HALF_UP)

    def display_money(value):
        return f"₹{money(value):,.2f}"

    def deduction_detail(
        deduction_type,
        amount,
        day,
        reason,
        policy,
        calculation,
        leave_type=None,
        rate_label="Normal",
    ):
        affected_dates = [day]
        return {
            "deduction_type": deduction_type,
            "type": deduction_type,
            "amount": money(amount),
            "amount_display": display_money(amount),
            "affected_days": 1,
            "days": 1,
            "affected_dates": affected_dates,
            "dates": affected_dates,
            "date": day,
            "day": day.strftime("%A"),
            "leave_type": leave_type or deduction_type,
            "rate_label": rate_label,
            "reason": reason,
            "policy": policy,
            "calculation": calculation,
        }

    deduction_details = []
    for day in sorted(absence_only_dates):
        deduction_details.append(deduction_detail(
            "Absence deduction", daily_rate, day,
            "No validated attendance and no approved leave covered this working day.",
            "Absent working days are unpaid.",
            "1 × daily salary", "Absence",
        ))
    for day in sorted(normal_lwp_dates):
        ordinal = sorted(lwp_dates_year).index(day) + 1
        deduction_details.append(deduction_detail(
            "LWP deduction", daily_rate, day,
            f"{ordinal}th LWP day in the leave year. Within the annual 12-day allowance, so normal deduction applies.",
            "LWP allowance = 12 days/year; days 1–12 use normal deduction.",
            "1 × daily salary", "LWP", "Normal",
        ))
    for day in sorted(double_lwp_dates):
        ordinal = sorted(lwp_dates_year).index(day) + 1
        deduction_details.append(deduction_detail(
            "Double-LWP deduction", daily_rate * 2, day,
            f"{ordinal}th LWP day in the leave year. The first 12 LWP days are exhausted, so double deduction applies.",
            "LWP days 1–12 = normal deduction; LWP day 13+ = double deduction.",
            "1 × daily salary × 2", "LWP", "Double",
        ))
    for day in sorted(other_unpaid_dates):
        deduction_details.append(deduction_detail(
            "Other unpaid leave", daily_rate, day,
            "Leave was not approved or was not an eligible paid leave type.",
            "Unapproved or ineligible leave is treated as unpaid.",
            "1 × daily salary", "Unpaid leave",
        ))
    for day in sorted(sandwich_dates):
        deduction_details.append(deduction_detail(
            "Sandwich deduction", daily_rate, day,
            "Saturday and Monday leave caused this Sunday to be deducted under the sandwich rule.",
            "Saturday plus Monday leave causes Sunday deduction.",
            "1 × daily salary", "Sandwich Sunday",
        ))
    total_deductions = sum(
        (item["amount"] for item in deduction_details),
        Decimal("0.00"),
    )
    absence_deduction = sum(
        (item["amount"] for item in deduction_details if item["deduction_type"] == "Absence deduction"),
        Decimal("0.00"),
    )
    lwp_deduction = sum(
        (item["amount"] for item in deduction_details if item["deduction_type"] == "LWP deduction"),
        Decimal("0.00"),
    )
    double_lwp_deduction = sum(
        (item["amount"] for item in deduction_details if item["deduction_type"] == "Double-LWP deduction"),
        Decimal("0.00"),
    )
    other_unpaid_deduction = sum(
        (item["amount"] for item in deduction_details if item["deduction_type"] == "Other unpaid leave"),
        Decimal("0.00"),
    )
    sandwich_deduction = sum(
        (item["amount"] for item in deduction_details if item["deduction_type"] == "Sandwich deduction"),
        Decimal("0.00"),
    )
    net_pay = salary - total_deductions

    return {
        "start_date": start_date,
        "end_date": end_date,
        "current_salary": money(salary),
        "basic_salary": money(salary),
        "basic_salary_display": display_money(salary),
        "paid_days": len(paid_dates | (attendance_dates & working_set)),
        "unpaid_days": len(unpaid_dates),
        "paid_leave_days": len(paid_dates),
        "unpaid_leave_days": len(unpaid_dates - absence_only_dates),
        "absence_days": len(absence_only_dates),
        "lwp_days": len(normal_lwp_dates) + len(double_lwp_dates),
        "sandwich_days": len(sandwich_dates),
        "double_lwp_days": len(double_lwp_dates),
        "absence_deduction": money(absence_deduction),
        "lwp_deduction": money(lwp_deduction),
        "other_unpaid_deduction": money(other_unpaid_deduction),
        "double_lwp_deduction": money(double_lwp_deduction),
        "sandwich_deduction": money(sandwich_deduction),
        "total_affected_days": len(deduction_details),
        "total_deductions": money(total_deductions),
        "net_pay": money(net_pay),
        "net_pay_display": display_money(net_pay),
        "total_deductions_display": display_money(total_deductions),
        "deduction_details": deduction_details,
    }


def employee_payslips(employee):
    """Return only payslips belonging to the authenticated employee."""
    return Payslip.objects.filter(employee_id=employee).order_by("-end_date", "-id")


def create_current_payslip(employee):
    """Persist a deterministic current-period payroll snapshot."""
    result = calculate_employee_payroll(employee)
    snapshot_deductions = [
        {
            **detail,
            "amount": float(detail["amount"]),
            "affected_dates": [day.isoformat() for day in detail["affected_dates"]],
            "dates": [day.isoformat() for day in detail["dates"]],
        }
        for detail in result["deduction_details"]
    ]
    pay_head_data = {
        "start_date": result["start_date"].isoformat(),
        "end_date": result["end_date"].isoformat(),
        "basic_pay": float(result["basic_salary"]),
        "gross_pay": float(result["basic_salary"]),
        "contract_wage": float(result["basic_salary"]),
        "net_pay": float(result["net_pay"]),
        "total_deductions": float(result["total_deductions"]),
        "loss_of_pay": float(result["total_deductions"]),
        "allowances": [],
        "basic_pay_deductions": [],
        "gross_pay_deductions": [],
        "pretax_deductions": [],
        "post_tax_deductions": [],
        "tax_deductions": [],
        "net_deductions": [],
        "federal_tax": 0,
        "paid_days": result["paid_days"],
        "unpaid_days": result["unpaid_days"],
        "absence_days": result["absence_days"],
        "lwp_days": result["lwp_days"],
        "double_lwp_days": result["double_lwp_days"],
        "sandwich_days": result["sandwich_days"],
        "deduction_details": snapshot_deductions,
    }
    payslip, _ = Payslip.objects.get_or_create(
        employee_id=employee,
        start_date=result["start_date"],
        end_date=result["end_date"],
        defaults={
            "pay_head_data": pay_head_data,
            "contract_wage": result["basic_salary"],
            "basic_pay": result["basic_salary"],
            "gross_pay": result["basic_salary"],
            "deduction": result["total_deductions"],
            "net_pay": result["net_pay"],
            "status": "draft",
        },
    )
    return payslip
