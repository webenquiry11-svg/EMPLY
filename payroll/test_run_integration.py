import json
from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.test import Client, TestCase
from django.urls import reverse

from base.models import Company
from employee.models import Employee, EmployeeWorkInformation
from payroll.models.models import Contract, Payslip, PayrollApprovalEvent, PayrollRun


class PayrollRunIntegrationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        user_model = get_user_model()
        cls.admin = user_model.objects.create_superuser(
            username="payroll-superuser",
            email="payroll-superuser@example.test",
            password="test-password",
        )
        cls.company = Company.objects.create(
            company="Payroll Integration Co",
            address="1 Payroll Way",
            country="Testland",
            state="Test State",
            city="Test City",
            zip="00000",
        )
        cls.employee = Employee.objects.create(
            employee_first_name="Test",
            employee_last_name="Employee",
            email="employee@example.test",
            phone="5550100",
        )
        work_info, _ = EmployeeWorkInformation.objects.get_or_create(
            employee_id=cls.employee
        )
        work_info.company_id_id = cls.company.pk
        work_info.save()
        cls.employee.refresh_from_db()

    def setUp(self):
        self.client.force_login(self.admin)
        self.employee = Employee.objects.get(pk=self.employee.pk)
        self.period_end = date.today() - timedelta(days=1)
        self.period_start = self.period_end.replace(day=1)
        Contract.objects.create(
            contract_name="Integration test contract",
            employee_id=self.employee,
            contract_start_date=self.period_start - timedelta(days=1),
            contract_status="active",
            wage=1000,
        )

    def _create_run(self):
        employee = Employee.objects.get(pk=self.employee.pk)
        self.assertEqual(
            employee.employee_work_info.company_id_id,
            self.company.pk,
            (
                employee.employee_work_info.pk,
                employee.employee_work_info.employee_id_id,
                EmployeeWorkInformation._base_manager.filter(
                    pk=employee.employee_work_info.pk
                ).values_list("company_id_id", flat=True).first(),
            ),
        )
        calculation = {
            "json_data": json.dumps({"contract_wage": 1000, "components": []}),
            "installments": [],
            "basic_pay": 1000,
            "gross_pay": 1000,
            "total_deductions": 100,
            "net_pay": 900,
        }
        with (
            patch(
                "payroll.views.component_views.payroll_calculation",
                return_value=calculation,
            ),
            patch("payroll.views.component_views.calculate_employer_contribution"),
        ):
            return self.client.post(
                reverse("payroll-run-create"),
                {
                    "company": self.company.pk,
                    "start_date": self.period_start.isoformat(),
                    "end_date": self.period_end.isoformat(),
                    "employees": [self.employee.pk],
                },
            )

    def test_run_preview_submission_approval_and_finalization_use_snapshots(self):
        response = self._create_run()

        self.assertEqual(response.status_code, 302)
        run = PayrollRun.objects.get()
        self.assertEqual(run.status, "draft")
        self.assertEqual(run.items.count(), 1)
        item = run.items.get()
        self.assertEqual(item.net_pay, 900)
        self.assertEqual(item.snapshot["pay_data"]["contract_wage"], 1000)

        submit_url = reverse("payroll-run-action", args=[run.pk, "submit"])
        approve_url = reverse("payroll-run-action", args=[run.pk, "approve"])
        finalize_url = reverse("payroll-run-action", args=[run.pk, "finalize"])
        self.client.post(submit_url)
        run.refresh_from_db()
        self.assertEqual(run.status, "submitted")
        payslip = Payslip.objects.get()
        self.assertEqual(payslip.status, "review_ongoing")
        self.assertEqual(payslip.net_pay, 900)

        self.client.post(approve_url)
        run.refresh_from_db()
        payslip.refresh_from_db()
        self.assertEqual(run.status, "approved")
        self.assertEqual(payslip.status, "confirmed")

        self.client.post(finalize_url)
        run.refresh_from_db()
        payslip.refresh_from_db()
        self.assertEqual(run.status, "finalized")
        self.assertEqual(payslip.status, "confirmed")
        self.assertEqual(payslip.net_pay, 900)
        self.assertEqual(payslip.pay_head_data, item.snapshot["pay_data"])
        events = list(run.approval_events.all())
        self.assertEqual(
            [(event.from_status, event.to_status) for event in events],
            [
                ("", "draft"),
                ("draft", "submitted"),
                ("submitted", "approved"),
                ("approved", "finalized"),
            ],
        )
        self.assertTrue(
            all(event.actor_id == self.admin.pk and event.created_at for event in events)
        )
        self.assertEqual(PayrollApprovalEvent.objects.count(), 4)

        payslip.net_pay = 1
        with self.assertRaises(ValidationError):
            payslip.save()
        payslip.refresh_from_db()
        self.assertEqual(payslip.net_pay, 900)

    def test_preview_persists_authoritative_payroll_calculation(self):
        with patch("payroll.views.component_views.calculate_employer_contribution"):
            response = self.client.post(
                reverse("payroll-run-create"),
                {
                    "company": self.company.pk,
                    "start_date": self.period_start.isoformat(),
                    "end_date": self.period_end.isoformat(),
                    "employees": [self.employee.pk],
                },
            )

        self.assertEqual(response.status_code, 302)
        item = PayrollRun.objects.get().items.get()
        pay_data = item.snapshot["pay_data"]
        self.assertEqual(pay_data["contract_wage"], 1000)
        self.assertEqual(item.basic_pay, pay_data["basic_pay"])
        self.assertEqual(item.gross_pay, pay_data["gross_pay"])
        self.assertEqual(item.deduction, pay_data["total_deductions"])
        self.assertEqual(item.net_pay, pay_data["net_pay"])
        self.assertEqual(pay_data["snapshot_source"], "official_payroll_calculation")

    def test_duplicate_period_is_rejected_after_a_run_exists(self):
        response = self._create_run()
        self.assertEqual(response.status_code, 302)

        response = self._create_run()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(PayrollRun.objects.count(), 1)

    def test_invalid_transition_does_not_change_run_or_history(self):
        response = self._create_run()
        self.assertEqual(response.status_code, 302)
        run = PayrollRun.objects.get()

        response = self.client.post(
            reverse("payroll-run-action", args=[run.pk, "finalize"])
        )

        self.assertEqual(response.status_code, 302)
        run.refresh_from_db()
        self.assertEqual(run.status, "draft")
        self.assertEqual(run.approval_events.count(), 1)

    def test_user_without_payroll_permission_cannot_submit_run(self):
        response = self._create_run()
        self.assertEqual(response.status_code, 302)
        run = PayrollRun.objects.get()
        unauthorized = get_user_model().objects.create_user(
            username="unprivileged-payroll-user",
            password="test-password",
        )
        self.client.force_login(unauthorized)

        response = self.client.post(
            reverse("payroll-run-action", args=[run.pk, "submit"])
        )

        self.assertEqual(response.status_code, 403)
        run.refresh_from_db()
        self.assertEqual(run.status, "draft")
        self.assertEqual(run.approval_events.count(), 1)

    def test_employee_cannot_download_another_employees_payslip(self):
        other_employee = Employee.objects.create(
            employee_first_name="Other",
            employee_last_name="Employee",
            email="other-employee@example.test",
            phone="5550101",
        )
        work_info, _ = EmployeeWorkInformation.objects.get_or_create(
            employee_id=other_employee
        )
        work_info.company_id_id = self.company.pk
        work_info.save()
        payslip = Payslip.objects.create(
            employee_id=other_employee,
            start_date=self.period_start,
            end_date=self.period_end,
            pay_head_data={},
        )
        employee_user = self.employee.employee_user_id
        employee_user.is_new_employee = False
        employee_user.save(update_fields=["is_new_employee"])
        employee_client = Client()
        employee_client.force_login(employee_user)

        for route_name, argument in (
            ("view-payslip-pdf", payslip.pk),
            ("payslip-pdf", payslip.pk),
        ):
            response = employee_client.get(reverse(route_name, args=[argument]))
            self.assertEqual(response.status_code, 302)

    def test_employee_dashboard_finds_finalized_snapshot_beyond_recent_history(self):
        period_start = date(2025, 1, 1)
        period_end = date(2025, 1, 31)
        snapshot = {
            "start_date": period_start.isoformat(),
            "end_date": period_end.isoformat(),
            "paid_days": 22,
            "unpaid_days": 0,
            "allowances": [],
            "basic_pay_deductions": [],
            "gross_pay_deductions": [],
            "pretax_deductions": [],
            "post_tax_deductions": [],
            "tax_deductions": [],
            "net_deductions": [],
        }
        finalized = Payslip._base_manager.create(
            employee_id=self.employee,
            start_date=period_start,
            end_date=period_end,
            pay_head_data=snapshot,
            basic_pay=4400,
            gross_pay=4500,
            deduction=200,
            net_pay=4300,
            status="confirmed",
        )
        for month in range(2, 8):
            Payslip._base_manager.create(
                employee_id=self.employee,
                start_date=date(2025, month, 1),
                end_date=date(2025, month, 28),
                pay_head_data={},
                basic_pay=1000,
                gross_pay=1000,
                deduction=0,
                net_pay=1000,
                status="draft",
            )
        payroll = {
            "start_date": period_start,
            "end_date": period_end,
            "basic_salary": 5000,
            "current_salary": 5000,
            "gross_pay": 5000,
            "total_deductions": 300,
            "net_pay": 4700,
            "basic_salary_display": "₹5,000.00",
            "total_deductions_display": "₹300.00",
            "net_pay_display": "₹4,700.00",
            "paid_days": 20,
            "unpaid_days": 11,
            "paid_leave_days": 0,
            "unpaid_leave_days": 0,
            "absence_days": 11,
            "sandwich_days": 0,
            "daily_rate_basis": "Estimate",
            "deduction_details": [],
            "total_affected_days": 0,
        }
        employee_user = self.employee.employee_user_id
        employee_user.is_new_employee = False
        employee_user.save(update_fields=["is_new_employee"])
        employee_client = Client()
        employee_client.force_login(employee_user)

        with (
            patch(
                "payroll.views.views.calculate_employee_payroll",
                return_value=payroll,
            ),
            patch(
                "payroll.views.views.render",
                return_value=HttpResponse("ok"),
            ) as render_mock,
        ):
            response = employee_client.get(reverse("payroll-dashboard"))

        self.assertEqual(response.status_code, 200)
        context = render_mock.call_args.kwargs["context"]
        self.assertTrue(context["payroll_is_finalized"])
        self.assertEqual(context["finalized_payslip"].pk, finalized.pk)
        self.assertEqual(context["payroll"]["net_pay"], 4300)


class SuperAdminManagementIntegrationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        user_model = get_user_model()
        cls.superuser = user_model.objects.create_superuser(
            username="management-superuser",
            email="management-superuser@example.test",
            password="test-password",
        )

    def setUp(self):
        self.client.force_login(self.superuser)

    def test_superuser_without_employee_profile_can_access_console(self):
        self.assertFalse(hasattr(self.superuser, "employee_get"))

        response = self.client.get(reverse("super-admin-dashboard"))

        self.assertEqual(response.status_code, 200)

    def test_superuser_can_create_admin_and_assign_group_permissions(self):
        response = self.client.post(
            reverse("super-admin-users"),
            {
                "username": "managed-admin",
                "email": "managed-admin@example.test",
                "first_name": "Managed",
                "last_name": "Admin",
                "password1": "A-secure-test-password-239!",
                "password2": "A-secure-test-password-239!",
                "is_active": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        managed_admin = get_user_model().objects.get(username="managed-admin")
        self.assertTrue(managed_admin.is_active)
        self.assertFalse(managed_admin.is_superuser)

        permission = Permission.objects.get(
            content_type__app_label="base",
            codename="change_company",
        )
        response = self.client.post(
            reverse("super-admin-groups"),
            {"name": "Payroll Operators", "permissions": [permission.pk]},
        )

        self.assertEqual(response.status_code, 302)
        group = Group.objects.get(name="Payroll Operators")
        self.assertTrue(group.permissions.filter(pk=permission.pk).exists())
