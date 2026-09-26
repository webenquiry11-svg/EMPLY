from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

import leave.forms as leave_forms
from leave.forms import _policy_leave_validation
from leave.models import AvailableLeave, LeaveType


class LeavePolicyBoundaryTests(SimpleTestCase):
    def employee(self):
        return SimpleNamespace(
            employee_work_info=SimpleNamespace(
                employee_type_id=SimpleNamespace(employee_type="Permanent"),
                company_id=None,
            )
        )

    def leave_type(self, name, approval="yes", compensatory=False):
        return SimpleNamespace(
            name=name,
            require_approval=approval,
            is_compensatory_leave=compensatory,
            payment="paid",
        )

    def validate(self, name, start, end=None, approval="yes", attachment=None, certificate=None, overlap=False):
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
                    "employee_id": self.employee(),
                    "leave_type_id": self.leave_type(name, approval),
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

    def test_paid_leave_combinations_are_rejected(self):
        start = date.today() + timedelta(days=7)
        for name in ("CL", "SL", "AL"):
            with self.assertRaises(ValidationError):
                self.validate(name, start, overlap=True)

    def test_sick_leave_is_allowed_with_or_without_optional_certificate(self):
        start = date.today() + timedelta(days=7)
        self.validate("SL", start, start + timedelta(days=1))
        self.validate("SL", start, start + timedelta(days=2))
        self.validate("SL", start, start + timedelta(days=1), certificate=object())
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

# Create your tests here.
