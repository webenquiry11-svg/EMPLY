import json
from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.test import Client, TestCase
from django.urls import reverse

from notifications.signals import notify
from base.models import Company
from employee.models import Employee, EmployeeWorkInformation
from horilla import horilla_middlewares
from payroll.models.models import Contract, Payslip, PayrollApprovalEvent, PayrollRun
from payroll.notifications import send_payroll_notification


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
        self.assertTrue(
            self.admin.notifications.filter(data__payroll_event=True).exists()
        )
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
        self.assertTrue(
            self.employee.employee_user_id.notifications.filter(
                data__payroll_event=True,
                verb__contains="ready for review",
            ).exists()
        )
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
        self.assertTrue(
            self.employee.employee_user_id.notifications.filter(
                data__payroll_event=True,
                verb__contains="finalized",
            ).exists()
        )
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
        historical = Payslip._base_manager.create(
            employee_id=self.employee,
            start_date=date(2024, 12, 1),
            end_date=date(2024, 12, 31),
            pay_head_data={
                "basic_pay_deductions": [
                    {"title": "Attendance deduction", "amount": 125}
                ]
            },
            basic_pay=4200,
            gross_pay=4300,
            deduction=125,
            net_pay=4175,
            status="paid",
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
                side_effect=lambda *_args, **_kwargs: payroll.copy(),
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
        self.assertEqual(len(context["payslips"]), 8)
        available_periods = {option["key"] for option in context["period_options"]}
        self.assertIn("2024-12", available_periods)
        self.assertIn("2025-01", available_periods)
        self.assertIn("2025-02", available_periods)
        with (
            patch(
                "payroll.views.views.calculate_employee_payroll",
                side_effect=lambda *_args, **_kwargs: payroll.copy(),
            ),
            patch(
                "payroll.views.views.render",
                return_value=HttpResponse("ok"),
            ) as historical_render,
        ):
            response = employee_client.get(
                reverse("payroll-dashboard"), {"period": "2024-12"}
            )
            self.assertEqual(response.status_code, 200)
            context = historical_render.call_args.kwargs["context"]
            self.assertEqual(context["selected_period"], "2024-12")
            self.assertEqual(context["current_month"], "December 2024")
            self.assertEqual(context["finalized_payslip"].pk, historical.pk)
            self.assertEqual(context["payroll"]["net_pay"], 4175)
            self.assertEqual(context["payroll"]["total_deductions"], 125)
            self.assertEqual(context["selected_month_payslips"], [historical])
            self.assertFalse(context["selected_is_current_period"])

            response = employee_client.get(
                reverse("payroll-dashboard"), {"period": "2025-07"}
            )
            self.assertEqual(response.status_code, 200)
            context = historical_render.call_args.kwargs["context"]
            self.assertTrue(context["payroll_has_snapshot"])
            self.assertFalse(context["payroll_is_finalized"])
            self.assertEqual(context["selected_payslip"].status, "draft")
            self.assertEqual(
                context["payroll"]["daily_rate_basis"],
                "Amounts and days below are read from the stored draft payslip.",
            )
            self.assertEqual(
                [payslip.start_date.strftime("%Y-%m") for payslip in context["selected_month_payslips"]],
                ["2025-07"],
            )
            self.assertEqual(context["payroll"]["net_pay"], 1000)


class PayrollDashboardRoleRoutingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        user_model = get_user_model()
        cls.superuser = user_model.objects.create_superuser(
            username="payroll-dashboard-superuser",
            email="payroll-dashboard-superuser@example.test",
            password="Test-password-239!",
        )
        cls.payroll_admin = user_model.objects.create_user(
            username="payroll-dashboard-admin",
            password="Test-password-239!",
        )
        permission = Permission.objects.get(
            content_type__app_label="payroll",
            codename="view_contract",
        )
        cls.payroll_admin.user_permissions.add(permission)

    @classmethod
    def tearDownClass(cls):
        horilla_middlewares._thread_locals.request = None
        super().tearDownClass()

    def test_payroll_management_dashboard_uses_dedicated_ui_for_superuser(self):
        client = Client()
        client.force_login(self.superuser)

        response = client.get(reverse("payroll-management-dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "payroll/management_dashboard.html")
        self.assertContains(response, "Good Morning,")
        self.assertContains(response, "Dashboard")
        self.assertNotContains(response, '>Overview</a>')
        self.assertContains(response, "data-payroll-notification-widget")
        self.assertContains(response, 'aria-label="Payroll notifications"')

    def test_payroll_management_dashboard_uses_dedicated_ui_for_admin(self):
        client = Client()
        client.force_login(self.payroll_admin)

        response = client.get(reverse("payroll-management-dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "payroll/management_dashboard.html")
        self.assertContains(response, "Good Morning,")

    def test_notification_feed_is_payroll_scoped_and_read_is_recipient_scoped(self):
        client = Client()
        client.force_login(self.superuser)
        notification = send_payroll_notification(
            self.superuser,
            self.superuser,
            "A payroll item is ready.",
            redirect="/payroll/dashboard/",
            icon="cash-outline",
        )[0][1][0]
        notify.send(
            self.superuser,
            recipient=self.superuser,
            verb="A non-payroll notification.",
        )

        response = client.get(reverse("payroll-notification-feed"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["unread_count"], 1)
        self.assertEqual(payload["notifications"][0]["id"], notification.pk)
        self.assertEqual(payload["notifications"][0]["url"], "/payroll/dashboard/")

        other_user_notification = send_payroll_notification(
            self.superuser,
            self.payroll_admin,
            "A private payroll notification.",
        )[0][1][0]
        self.assertEqual(
            client.post(
                reverse(
                    "payroll-notification-read",
                    args=[other_user_notification.pk],
                )
            ).status_code,
            404,
        )

        response = client.post(
            reverse("payroll-notification-read", args=[notification.pk])
        )

        self.assertEqual(response.status_code, 200)
        notification.refresh_from_db()
        self.assertFalse(notification.unread)

    def test_payroll_management_dashboard_month_selector_updates_period(self):
        client = Client()
        client.force_login(self.superuser)

        response = client.get(
            reverse("payroll-management-dashboard"),
            {"period": "2025-02"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "February 2025")
        self.assertContains(response, 'value="2025-02" selected')

    def test_payroll_workspace_sections_render_inside_the_dashboard(self):
        client = Client()
        client.force_login(self.superuser)
        sections = {
            "payslips": "Employee payslips",
            "runs": "Payroll runs",
            "contracts": "Contracts",
            "employees": "Employee directory",
            "reports": "Payroll reports",
            "allowances": "Allowances",
            "deductions": "Deductions",
            "reimbursements": "Reimbursements",
            "settings": "Payroll settings",
        }

        for section, heading in sections.items():
            with self.subTest(section=section):
                response = client.get(
                    reverse("payroll-management-dashboard"),
                    {"section": section},
                )
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, heading)
                self.assertContains(response, 'data-section="' + section + '"')

        self.assertNotContains(response, ">Loans</a>")

    def test_deduction_section_selects_employee_and_monthly_policy_details(self):
        employee = Employee.objects.create(
            employee_first_name="Policy",
            employee_last_name="Employee",
            email="policy-employee@example.test",
            phone="5550123",
        )
        work_info, _ = EmployeeWorkInformation.objects.get_or_create(
            employee_id=employee
        )
        work_info.basic_salary = 20000
        work_info.save()
        client = Client()
        client.force_login(self.superuser)
        url = reverse("payroll-management-dashboard")

        employee_list = client.get(url, {"section": "deductions"})

        self.assertEqual(employee_list.status_code, 200)
        self.assertContains(employee_list, "Policy Employee")
        self.assertContains(employee_list, f"employee={employee.pk}")

        details = client.get(
            url,
            {
                "section": "deductions",
                "employee": employee.pk,
                "period": "2026-09",
            },
        )

        self.assertEqual(details.status_code, 200)
        self.assertContains(details, "Monthly deductions")
        self.assertContains(details, "View month")
        self.assertContains(details, 'value="2026-09" selected')
        self.assertContains(details, "Leave and attendance deductions for")
        self.assertContains(details, "Reason and policy")
        self.assertContains(details, "Calculation")

    def test_payroll_quick_actions_stay_in_workspace_or_open_workspace_modal(self):
        client = Client()
        client.force_login(self.superuser)

        response = client.get(reverse("payroll-management-dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'data-workspace-modal-url="'
            + reverse("payroll-run-create")
            + '"',
        )
        self.assertContains(
            response,
            'data-workspace-modal-url="'
            + reverse("generate-payslip")
            + '"',
        )
        self.assertContains(response, 'href="?section=employees&amp;period=')
        self.assertContains(response, 'href="?section=reports&amp;period=')
        self.assertContains(response, 'id="workspace-action-modal"')
        self.assertNotContains(response, 'class="quick-link" href="/employee/')

    def test_payroll_workspace_sections_respect_permissions(self):
        client = Client()
        client.force_login(self.payroll_admin)

        for section in ("reimbursements", "employees", "reports"):
            with self.subTest(section=section):
                response = client.get(
                    reverse("payroll-management-dashboard"),
                    {"section": section},
                )
                self.assertEqual(response.status_code, 403)

    def test_payroll_login_mode_opens_dedicated_management_dashboard(self):
        password = "Test-password-239!"
        for user in (self.superuser, self.payroll_admin):
            response = Client().post(
                reverse("login"),
                {
                    "username": user.username,
                    "password": password,
                    "login_mode": "payroll",
                },
            )
            self.assertRedirects(
                response,
                reverse("payroll-management-dashboard"),
                fetch_redirect_response=False,
            )


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
