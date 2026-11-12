from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

import leave.forms as leave_forms
from leave.forms import _policy_leave_validation
from leave.models import AvailableLeave, LeaveRequest, LeaveType


class LeavePolicyBoundaryTests(SimpleTestCase):
    def employee(self, employee_type="Permanent", employment_status="permanent"):
        return SimpleNamespace(
            employment_status=employment_status,
            employee_work_info=SimpleNamespace(
                employee_type_id=SimpleNamespace(employee_type=employee_type),
                company_id=None,
            )
        )

    def leave_type(self, name, approval="yes", compensatory=False, leave_unit="day"):
        return SimpleNamespace(
            name=name,
            require_approval=approval,
            is_compensatory_leave=compensatory,
            payment="paid",
            leave_unit=leave_unit,
        )

    def validate(self, name, start, end=None, approval="yes", attachment=None, certificate=None, overlap=False, employee_type="Permanent", employment_status="permanent", leave_unit="day"):
        empty = SimpleNamespace(
            values_list=lambda *args, **kwargs: [],
            exclude=lambda **kwargs: SimpleNamespace(exists=lambda: overlap),
            exists=lambda: False,
        )
        with patch.object(leave_forms.Holidays.objects, "filter", return_value=empty), patch.object(
            leave_forms.LeaveRequest.objects, "filter", return_value=empty
        ):
            _policy_leave_validation(
                {
                    "employee_id": self.employee(employee_type, employment_status),
                    "leave_type_id": self.leave_type(name, approval, leave_unit=leave_unit),
                    "start_date": start,
                    "end_date": end or start,
                    "attachment": attachment,
                    "doctor_certificate": certificate,
                }
            )

    def test_cl_and_al_notice_boundaries(self):
        with self.assertRaises(ValidationError):
            self.validate("CL", date.today() + timedelta(days=3))
        with self.assertRaises(ValidationError):
            self.validate("AL", date.today() + timedelta(days=6))
        self.validate("CL", date.today() + timedelta(days=4))
        self.validate("AL", date.today() + timedelta(days=7))

    def test_long_cl_requires_approval(self):
        with self.assertRaises(ValidationError):
            self.validate("CL", date.today() + timedelta(days=5),
                          date.today() + timedelta(days=7), approval="no")
        self.validate("CL", date.today() + timedelta(days=5),
                      date.today() + timedelta(days=7), approval="yes")

    def test_short_leave_is_permanent_status_only(self):
        start = date.today()
        self.validate("Short Leave", start, employment_status="permanent", leave_unit="minute")
        for status in ("active", "probation", "notice_period", "suspended", "inactive"):
            with self.subTest(status=status):
                with self.assertRaises(ValidationError):
                    self.validate("Short Leave", start, employment_status=status, leave_unit="minute")

    def test_contract_employees_can_request_paid_leave(self):
        self.validate(
            "CL",
            date.today() + timedelta(days=4),
            employee_type="Contract",
        )

    def test_interns_and_probationers_cannot_request_paid_leave(self):
        start = date.today() + timedelta(days=7)
        for employee_type in ("Intern", "Probationer"):
            with self.subTest(employee_type=employee_type):
                with self.assertRaises(ValidationError):
                    self.validate("SL", start, employee_type=employee_type)

    def test_paid_leave_combinations_are_rejected(self):
        start = date.today() + timedelta(days=7)
        for name in ("CL", "SL", "AL"):
            with self.assertRaises(ValidationError):
                self.validate(name, start, overlap=True)

    def test_sick_leave_certificate_is_required_after_two_days(self):
        start = date.today() + timedelta(days=7)
        self.validate("SL", start, start + timedelta(days=1))
        self.validate("SL", start, start + timedelta(days=1), certificate=object())
        with self.assertRaises(ValidationError):
            self.validate("SL", start, start + timedelta(days=2))
        self.validate("SL", start, start + timedelta(days=2), certificate=object())

    def test_al_weekend_requires_explicit_approval(self):
        saturday = date.today() + timedelta(days=(5 - date.today().weekday()) % 7 + 7)
        with self.assertRaises(ValidationError):
            self.validate("AL", saturday, saturday, approval="no")
        self.validate("AL", saturday, saturday, approval="yes")

    def test_ccl_exactly_90_days_requires_approved_weekend_work(self):
        employee = self.employee()
        leave_type = self.leave_type("CCL", compensatory=True)
        start = date.today() + timedelta(days=100)

        class CompensatoryQuery:
            def __init__(self, qualifying):
                self.qualifying = qualifying

            def filter(self, *args, **kwargs):
                return self

            def exists(self):
                return self.qualifying

        class CompensatoryManager:
            def __init__(self, qualifying_date):
                self.qualifying_date = qualifying_date

            def filter(self, **kwargs):
                return CompensatoryQuery(
                    self.qualifying_date >= kwargs["requested_date__gte"]
                    and self.qualifying_date <= kwargs["requested_date__lte"]
                )

        empty = SimpleNamespace(
            values_list=lambda *args, **kwargs: [],
            exclude=lambda **kwargs: SimpleNamespace(exists=lambda: False),
            exists=lambda: False,
        )
        with patch.object(
            leave_forms.apps,
            "get_model",
            return_value=SimpleNamespace(
                objects=CompensatoryManager(start - timedelta(days=90))
            ),
        ), patch.object(leave_forms.Holidays.objects, "filter", return_value=empty), patch.object(
            leave_forms.LeaveRequest.objects, "filter", return_value=empty
        ):
            _policy_leave_validation(
                {
                    "employee_id": employee,
                    "leave_type_id": leave_type,
                    "start_date": start,
                    "end_date": start,
                }
            )

        with patch.object(
            leave_forms.apps,
            "get_model",
            return_value=SimpleNamespace(
                objects=CompensatoryManager(start - timedelta(days=91))
            ),
        ), patch.object(leave_forms.Holidays.objects, "filter", return_value=empty), patch.object(
            leave_forms.LeaveRequest.objects, "filter", return_value=empty
        ):
            with self.assertRaises(ValidationError):
                _policy_leave_validation(
                    {
                        "employee_id": employee,
                        "leave_type_id": leave_type,
                        "start_date": start,
                        "end_date": start,
                    }
                )

    def test_annual_carryforward_is_applied_at_year_boundary(self):
        leave_type = LeaveType(
            carryforward_type="carryforward",
            carryforward_max=6,
            total_days=6,
        )
        balance = AvailableLeave(
            leave_type_id=leave_type,
            available_days=4,
            carryforward_days=0,
            total_leave_days=4,
        )
        balance.update_carryforward()
        self.assertEqual(balance.carryforward_days, 4)
        self.assertEqual(balance.available_days, 6)

    def test_permanent_yearly_leave_balance_accrues_monthly(self):
        employee = SimpleNamespace(
            employment_status="permanent",
            employee_work_info=SimpleNamespace(date_joining=date(2026, 1, 1)),
        )
        leave_type = SimpleNamespace(
            limit_leave=True,
            leave_unit="day",
            reset=True,
            reset_based="yearly",
            reset_month="1",
            reset_day="1",
            total_days=12,
            carryforward_type="no carryforward",
            carryforward_max=0,
        )
        balance = AvailableLeave(
            available_days=12,
            carryforward_days=0,
            assigned_date=date(2026, 1, 1),
        )
        balance._state.fields_cache["employee_id"] = employee
        balance._state.fields_cache["leave_type_id"] = leave_type

        self.assertTrue(balance.uses_monthly_accrual)
        self.assertEqual(balance.monthly_accrued_days(date(2026, 1, 31)), 1)
        self.assertEqual(balance.monthly_accrued_days(date(2026, 2, 1)), 2)
        self.assertEqual(balance.monthly_accrued_days(date(2026, 12, 31)), 12)

        approved_requests = []
        queryset = SimpleNamespace(
            values_list=lambda *args, **kwargs: approved_requests
        )
        with patch.object(
            LeaveRequest.objects, "filter", return_value=queryset
        ) as filter_requests:
            self.assertEqual(
                balance.effective_available_days(reference_date=date(2026, 1, 31)),
                1,
            )
            self.assertEqual(
                balance.effective_available_days(reference_date=date(2026, 2, 1)),
                2,
            )
            approved_requests.append((1, 1, 0))
            result = balance.effective_available_days(reference_date=date(2026, 2, 1))
            self.assertEqual(filter_requests.call_count, 3)
            self.assertEqual(result, 1)
            approved_requests.clear()
            with patch("leave.models.timezone.localdate", return_value=date(2026, 1, 31)):
                leave_request = SimpleNamespace(requested_days=1)
                self.assertTrue(balance.consume_monthly_accrual_balance(leave_request))
                self.assertEqual(leave_request.approved_available_days, 1)
                self.assertEqual(balance.available_days, 11)
                with self.assertRaises(ValidationError):
                    balance.consume_monthly_accrual_balance(
                        SimpleNamespace(requested_days=2)
                    )

        leave_type.reset_month = "2"
        leave_type.reset_day = "15"
        balance.assigned_date = date(2025, 2, 15)
        employee.employee_work_info.date_joining = date(2025, 2, 15)
        self.assertEqual(
            balance.monthly_accrued_days(date(2026, 2, 14)),
            12,
        )
        self.assertEqual(
            balance.monthly_accrued_days(date(2026, 2, 15)),
            1,
        )

    def test_monthly_accrual_does_not_apply_to_other_statuses_or_reset_periods(self):
        employee = SimpleNamespace(
            employment_status="probation",
            employee_work_info=SimpleNamespace(date_joining=date(2026, 1, 1)),
        )
        leave_type = SimpleNamespace(
            limit_leave=True,
            leave_unit="day",
            reset=True,
            reset_based="yearly",
            reset_month="1",
            reset_day="1",
            total_days=12,
            carryforward_type="no carryforward",
            carryforward_max=0,
        )
        balance = AvailableLeave(
            available_days=12,
            carryforward_days=2,
            total_leave_days=14,
            assigned_date=date(2026, 1, 1),
        )
        balance._state.fields_cache["employee_id"] = employee
        balance._state.fields_cache["leave_type_id"] = leave_type
        self.assertFalse(balance.uses_monthly_accrual)
        self.assertEqual(balance.effective_total_leave_days, 14)

        employee.employment_status = "permanent"
        leave_type.reset_based = "monthly"
        self.assertFalse(balance.uses_monthly_accrual)
        self.assertEqual(balance.effective_total_leave_days, 14)

# Create your tests here.
