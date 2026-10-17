from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.test import RequestFactory, SimpleTestCase, TestCase
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.test import APIRequestFactory, force_authenticate

from employee.models import Employee
from horilla_api.api_serializers.payroll.serializers import ReimbursementSerializer
from horilla_api.api_views.payroll.views import TaxBracketView
from payroll.models.models import Payslip
from payroll.views.run_views import _run_transition_error
from payroll.views.super_admin_views import super_admin_dashboard, super_admin_users
from payroll.views.views import _payslip_status_error
from base.views import login_user


class PayslipWorkflowSecurityTests(TestCase):
    def test_review_workflow_accepts_only_expected_transitions(self):
        payslip = SimpleNamespace(status="draft", get_status_display=lambda: "Draft")

        self.assertIsNone(_payslip_status_error(payslip, "review_ongoing"))
        self.assertIsNotNone(_payslip_status_error(payslip, "confirmed"))
        self.assertIsNotNone(_payslip_status_error(payslip, "paid"))

    def test_review_can_be_returned_to_draft_or_confirmed(self):
        payslip = SimpleNamespace(
            status="review_ongoing", get_status_display=lambda: "Review Ongoing"
        )

        self.assertIsNone(_payslip_status_error(payslip, "draft"))
        self.assertIsNone(_payslip_status_error(payslip, "confirmed"))

    def test_confirmed_payslip_can_only_be_marked_paid(self):
        payslip = SimpleNamespace(status="confirmed", get_status_display=lambda: "Confirmed")

        self.assertIsNone(_payslip_status_error(payslip, "paid"))
        self.assertIsNotNone(_payslip_status_error(payslip, "review_ongoing"))

    def test_paid_payslip_is_terminal(self):
        payslip = SimpleNamespace(status="paid", get_status_display=lambda: "Paid")

        self.assertIsNone(_payslip_status_error(payslip, "paid"))
        self.assertIsNotNone(_payslip_status_error(payslip, "confirmed"))

    def test_rejects_duplicate_even_when_only_one_matching_payslip_exists(self):
        payslip = Payslip(
            employee_id=Employee(id=1),
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
            pay_head_data={},
        )
        duplicate_query = Mock()
        duplicate_query.exclude.return_value = duplicate_query
        duplicate_query.exists.return_value = True

        with patch.object(Employee._base_manager, "select_for_update") as lock_employee:
            lock_employee.return_value.filter.return_value.first.return_value = Employee(
                id=1
            )
            with patch.object(
                Payslip._base_manager, "filter", return_value=duplicate_query
            ):
                with self.assertRaises(ValidationError):
                    payslip.save()


class PayrollRunTransitionTests(SimpleTestCase):
    def test_run_transitions_require_the_expected_source_state(self):
        self.assertIsNone(_run_transition_error("draft", "submit"))
        self.assertIsNone(_run_transition_error("submitted", "approve"))
        self.assertIsNone(_run_transition_error("submitted", "reject"))
        self.assertIsNone(_run_transition_error("approved", "finalize"))
        self.assertIsNotNone(_run_transition_error("finalized", "submit"))
        self.assertIsNotNone(_run_transition_error("rejected", "approve"))
        self.assertIsNotNone(_run_transition_error("finalized", "finalize"))


class PayrollRoleAccessTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_non_superuser_cannot_access_super_admin_dashboard_or_user_management(self):
        user = SimpleNamespace(
            is_authenticated=True,
            is_active=True,
            is_superuser=False,
        )
        dashboard_request = self.factory.get("/payroll/super-admin/")
        dashboard_request.user = user
        users_request = self.factory.get("/payroll/super-admin/users/")
        users_request.user = user

        self.assertEqual(super_admin_dashboard(dashboard_request).status_code, 403)
        self.assertEqual(super_admin_users(users_request).status_code, 403)

    def test_companyless_admin_scope_does_not_default_to_all_companies(self):
        from base.middleware import CompanyMiddleware, NO_COMPANY_SCOPE

        user = SimpleNamespace(
            is_anonymous=False,
            is_superuser=False,
            has_perm=lambda permission: False,
        )
        request = SimpleNamespace(user=user, session={})

        self.assertEqual(
            CompanyMiddleware(lambda request: None)._get_company_id(request),
            NO_COMPANY_SCOPE,
        )

    def test_shared_login_routes_superuser_and_payroll_admin_without_employee(self):
        class Session(dict):
            def set_expiry(self, value):
                self.expiry = value

        for user, expected_route in (
            (
                SimpleNamespace(
                    is_active=True,
                    is_superuser=True,
                    has_perm=lambda permission: False,
                ),
                "super-admin-dashboard",
            ),
            (
                SimpleNamespace(
                    is_active=True,
                    is_superuser=False,
                    has_perm=lambda permission: permission
                    == "payroll.view_contract",
                ),
                "view-payroll-dashboard",
            ),
        ):
            request = self.factory.post(
                "/login/",
                {
                    "username": "admin",
                    "password": "existing-password",
                    "login_mode": "payroll",
                },
            )
            request.session = Session()
            with (
                patch("base.views.authenticate", return_value=user),
                patch("base.views.login"),
                patch("base.views.messages.success"),
                patch("base.views.redirect") as redirect_mock,
            ):
                login_user(request)
            redirect_mock.assert_called_once_with(expected_route)

    def test_tax_bracket_api_requires_view_permission(self):
        user = SimpleNamespace(
            is_authenticated=True,
            has_perm=lambda permission: False,
        )
        request = APIRequestFactory().get("/api/payroll/tax-brackets/")
        force_authenticate(request, user=user)

        response = TaxBracketView.as_view()(request)

        self.assertEqual(response.status_code, 403)

    def test_new_reimbursement_cannot_be_submitted_as_approved(self):
        serializer = ReimbursementSerializer()

        with self.assertRaises(DRFValidationError):
            serializer.validate({"status": "approved"})

    def test_finalized_payslip_financial_values_cannot_be_changed(self):
        payslip = Payslip(
            id=10,
            employee_id=Employee(id=1),
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
            pay_head_data={"net_pay": 900},
            contract_wage=1000,
            basic_pay=1000,
            gross_pay=1000,
            deduction=100,
            net_pay=900,
            status="confirmed",
        )
        previous = SimpleNamespace(
            status="confirmed",
            employee_id_id=1,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
            pay_head_data={"net_pay": 1000},
            contract_wage=1000,
            basic_pay=1000,
            gross_pay=1000,
            deduction=0,
            net_pay=1000,
        )
        query = Mock()
        query.exclude.return_value = query
        query.exists.return_value = False
        query.first.return_value = previous

        with patch.object(Employee._base_manager, "select_for_update") as lock_employee:
            lock_employee.return_value.filter.return_value.first.return_value = Employee(
                id=1
            )
            with patch.object(Payslip._base_manager, "filter", return_value=query):
                with self.assertRaises(ValidationError):
                    payslip.save()
