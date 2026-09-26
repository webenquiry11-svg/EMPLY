from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from payroll import services


class PayrollPolicyTests(SimpleTestCase):
    class Query:
        def __init__(self, items):
            self.items = items

        def select_related(self, *args):
            return self

        def filter(self, **kwargs):
            if "leave_type_id__name__iregex" in kwargs:
                return self.__class__(
                    [
                        item
                        for item in self.items
                        if item.status == "approved"
                        and item.leave_type_id.name.lower() == "lwp"
                        and item.start_date <= kwargs.get("end_date", date.max)
                        and item.end_date >= kwargs.get("start_date", date.min)
                    ]
                )
            return self.__class__(
                [
                    item
                    for item in self.items
                    if item.start_date <= kwargs.get("end_date", date.max)
                    and item.end_date >= kwargs.get("start_date", date.min)
                ]
            )

        def first(self):
            return self.items[0] if self.items else None

        def __iter__(self):
            return iter(self.items)

    def setUp(self):
        self.employee = SimpleNamespace(
            employee_work_info=SimpleNamespace(
                basic_salary=3100,
                employee_type_id=SimpleNamespace(employee_type="Permanent"),
            ),
            contract_set=SimpleNamespace(filter=lambda **kwargs: self.Query([])),
        )
        self.cl = SimpleNamespace(
            name="CL", payment="paid", is_compensatory_leave=False
        )
        self.sl = SimpleNamespace(
            name="SL", payment="paid", is_compensatory_leave=False
        )
        self.al = SimpleNamespace(
            name="AL", payment="paid", is_compensatory_leave=False
        )
        self.ccl = SimpleNamespace(
            name="CCL", payment="paid", is_compensatory_leave=True
        )
        self.lwp = SimpleNamespace(
            name="LWP", payment="unpaid", is_compensatory_leave=False
        )

    def leave(self, leave_type, start_date, status="approved"):
        return SimpleNamespace(
            leave_type_id=leave_type,
            start_date=start_date,
            end_date=start_date,
            status=status,
            employee_id=self.employee,
        )

    def calculate(self, leaves, start_date=date(2026, 1, 1), end_date=date(2026, 1, 31), today=date(2026, 1, 31)):
        self.employee.leaverequest_set = self.Query(leaves)
        with patch.object(
            services,
            "_working_dates",
            lambda start, end: [
                day
                for day in services._dates(start, end)
                if day.weekday() < 5
            ],
        ), patch.object(
            services.timezone, "localdate", lambda: today
        ), patch.object(services.apps, "is_installed", lambda name: False):
            qualifying = SimpleNamespace(exists=lambda: True)
            qualifying.filter = lambda *args, **kwargs: qualifying
            with patch.object(
                services.apps,
                "get_model",
                lambda app, model: SimpleNamespace(
                    objects=SimpleNamespace(
                        filter=lambda **kwargs: qualifying
                    )
                ),
            ):
                return services.calculate_employee_payroll(
                    self.employee, start_date, end_date
                )

    def test_approved_paid_leave_is_not_absence(self):
        result = self.calculate([self.leave(self.cl, date(2026, 1, 5))])
        self.assertEqual(result["absence_days"], 21)
        self.assertEqual(result["paid_leave_days"], 1)
        self.assertEqual(result["total_deductions"], result["absence_deduction"] + result["other_unpaid_deduction"] + result["lwp_deduction"] + result["double_lwp_deduction"] + result["sandwich_deduction"])

    def test_valid_paid_leave_types_do_not_create_deduction_events(self):
        leaves = [
            self.leave(self.cl, date(2026, 1, 5)),
            self.leave(self.sl, date(2026, 1, 6)),
            self.leave(self.al, date(2026, 1, 7)),
            self.leave(self.ccl, date(2026, 1, 8)),
        ]
        result = self.calculate(leaves)
        self.assertEqual(result["paid_leave_days"], 4)
        deducted_dates = {event["date"] for event in result["deduction_details"]}
        self.assertTrue(
            deducted_dates.isdisjoint({leave.start_date for leave in leaves})
        )
        self.assertEqual(
            sum(event["amount"] for event in result["deduction_details"]),
            result["total_deductions"],
        )

    def test_thirteenth_lwp_day_is_double_rate(self):
        dates = [
            date(2026, 1, day)
            for day in range(1, 22)
            if date(2026, 1, day).weekday() < 5
        ][:13]
        result = self.calculate([self.leave(self.lwp, day) for day in dates])
        self.assertEqual(result["lwp_days"], 13)
        self.assertEqual(result["double_lwp_days"], 1)
        self.assertGreater(result["double_lwp_deduction"], 0)
        lwp_events = [
            event for event in result["deduction_details"]
            if event["leave_type"] == "LWP"
        ]
        self.assertEqual(len(lwp_events), 13)
        self.assertEqual(sum(event["amount"] for event in result["deduction_details"]), result["total_deductions"])
        self.assertEqual(lwp_events[-1]["rate_label"], "Double")
        self.assertEqual(result["net_pay"], result["basic_salary"] - result["total_deductions"])

    def test_first_lwp_day_is_normal_rate(self):
        result = self.calculate([self.leave(self.lwp, date(2026, 1, 5))])
        event = next(
            item for item in result["deduction_details"] if item["leave_type"] == "LWP"
        )
        self.assertEqual(event["rate_label"], "Normal")
        self.assertEqual(event["amount"], result["lwp_deduction"])

    def test_lwp_boundaries_have_accurate_ordinals_and_rates(self):
        dates = [
            date(2026, 1, day)
            for day in range(1, 22)
            if date(2026, 1, day).weekday() < 5
        ][:14]
        result = self.calculate([self.leave(self.lwp, day) for day in dates])
        events = [
            item for item in result["deduction_details"] if item["leave_type"] == "LWP"
        ]
        self.assertEqual(len(events), 14)
        self.assertIn("1th LWP day", events[0]["reason"])
        self.assertIn("12th LWP day", events[11]["reason"])
        self.assertIn("13th LWP day", events[12]["reason"])
        self.assertIn("14th LWP day", events[13]["reason"])
        self.assertTrue(all(item["rate_label"] == "Normal" for item in events[:12]))
        self.assertTrue(all(item["rate_label"] == "Double" for item in events[12:]))
        self.assertTrue(all(item["policy"] and item["calculation"] for item in events))

    def test_lwp_counter_spans_months(self):
        dates = [
            date(2026, 1, day)
            for day in range(1, 20)
            if date(2026, 1, day).weekday() < 5
        ][:10] + [
            date(2026, 2, day)
            for day in range(1, 10)
            if date(2026, 2, day).weekday() < 5
        ][:4]
        result = self.calculate(
            [self.leave(self.lwp, day) for day in dates],
            end_date=date(2026, 2, 28),
            today=date(2026, 2, 28),
        )
        feb_events = [
            item for item in result["deduction_details"]
            if item["date"].month == 2 and item["leave_type"] == "LWP"
        ]
        self.assertEqual(len(feb_events), 4)
        self.assertTrue(all(item["rate_label"] == "Double" for item in feb_events[-2:]))

    def test_deduction_events_have_unique_dates(self):
        dates = [
            date(2026, 1, day)
            for day in range(1, 20)
            if date(2026, 1, day).weekday() < 5
        ][:13]
        result = self.calculate([self.leave(self.lwp, day) for day in dates])
        event_dates = [item["date"] for item in result["deduction_details"]]
        self.assertEqual(len(event_dates), len(set(event_dates)))

    def test_non_permanent_categories_do_not_receive_paid_leave(self):
        for category in ("Contract", "Intern", "Probationary"):
            self.employee.employee_work_info.employee_type_id.employee_type = category
            result = self.calculate([self.leave(self.cl, date(2026, 1, 5))])
            self.assertEqual(result["paid_leave_days"], 0)
            self.assertEqual(result["unpaid_leave_days"], 1)
            self.assertTrue(
                any(item["date"] == date(2026, 1, 5)
                    for item in result["deduction_details"])
            )
        self.employee.employee_work_info.employee_type_id.employee_type = "Permanent"

    def test_lwp_counter_resets_at_new_leave_year(self):
        prior_year = [
            self.leave(self.lwp, date(2026, 12, day))
            for day in range(1, 18)
            if date(2026, 12, day).weekday() < 5
        ][:13]
        new_year_leave = self.leave(self.lwp, date(2027, 1, 4))
        result = self.calculate(
            prior_year + [new_year_leave],
            start_date=date(2027, 1, 1),
            end_date=date(2027, 1, 31),
            today=date(2027, 1, 31),
        )
        event = next(
            item for item in result["deduction_details"]
            if item["date"] == date(2027, 1, 4)
        )
        self.assertEqual(event["rate_label"], "Normal")

    def test_sandwich_sunday_is_a_separate_real_event(self):
        saturday = date(2026, 1, 10)
        monday = date(2026, 1, 12)
        result = self.calculate([
            self.leave(self.cl, saturday),
            self.leave(self.cl, monday),
        ])
        event = next(
            item
            for item in result["deduction_details"]
            if item["leave_type"] == "Sandwich Sunday"
        )
        self.assertEqual(event["date"], date(2026, 1, 11))
        self.assertEqual(event["amount"], result["sandwich_deduction"])

    def test_sandwich_does_not_apply_without_both_adjacent_leave_days(self):
        result = self.calculate([self.leave(self.cl, date(2026, 1, 10))])
        self.assertFalse(
            any(item["leave_type"] == "Sandwich Sunday"
                for item in result["deduction_details"])
        )

    def test_pending_leave_is_unpaid_once(self):
        result = self.calculate(
            [self.leave(self.cl, date(2026, 1, 5), status="requested")]
        )
        events = [
            item for item in result["deduction_details"]
            if item["date"] == date(2026, 1, 5)
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(result["unpaid_leave_days"], 1)

    def test_payslip_snapshot_uses_calculated_payroll_values(self):
        result = {
            "start_date": date(2026, 1, 1),
            "end_date": date(2026, 1, 31),
            "basic_salary": 3100,
            "net_pay": 2800,
            "total_deductions": 300,
            "deduction_details": [],
            "paid_days": 20,
            "unpaid_days": 2,
            "absence_days": 1,
            "lwp_days": 1,
            "double_lwp_days": 0,
            "sandwich_days": 0,
        }
        payslip_manager = SimpleNamespace(get_or_create=Mock(return_value=(SimpleNamespace(), True)))
        with patch.object(services, "calculate_employee_payroll", return_value=result), patch.object(
            services.Payslip, "objects", payslip_manager
        ) as manager:
            services.create_current_payslip(self.employee)
        defaults = manager.get_or_create.call_args.kwargs["defaults"]
        self.assertEqual(defaults["deduction"], result["total_deductions"])
        self.assertEqual(defaults["net_pay"], result["net_pay"])
        self.assertEqual(defaults["pay_head_data"]["lwp_days"], result["lwp_days"])

    def test_minute_short_leave_creates_no_deduction(self):
        short_leave = SimpleNamespace(
            name="Short Leave",
            leave_unit="minute",
            payment="paid",
            is_compensatory_leave=False,
        )
        baseline = self.calculate([])
        result = self.calculate([self.leave(short_leave, date(2026, 1, 5))])
        self.assertEqual(result["total_deductions"], baseline["total_deductions"])
        self.assertEqual(result["lwp_days"], 0)
        self.assertEqual(result["absence_days"], baseline["absence_days"])

    def test_rejected_leave_is_not_paid_or_double_counted(self):
        result = self.calculate(
            [self.leave(self.cl, date(2026, 1, 5), status="rejected")]
        )
        self.assertEqual(result["paid_leave_days"], 0)
        self.assertEqual(result["absence_days"], 21)
        self.assertEqual(result["unpaid_leave_days"], 1)
