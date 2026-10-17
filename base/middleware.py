"""
middleware.py
"""

from django.apps import apps
from django.core.cache import cache
from django.db.models import Q
from django.shortcuts import redirect
from django.utils.translation import gettext_lazy as _

from base.backends import ConfiguredEmailBackend
from base.context_processors import AllCompany
from base.horilla_company_manager import HorillaCompanyManager
from base.models import Company, ShiftRequest, WorkTypeRequest
from employee.models import (
    DisciplinaryAction,
    Employee,
    EmployeeBankDetails,
    EmployeeWorkInformation,
)
from horilla.horilla_apps import TWO_FACTORS_AUTHENTICATION
from horilla.horilla_settings import APPS
from horilla.methods import get_horilla_model_class
from horilla_documents.models import DocumentRequest

CACHE_KEY = "horilla_company_models_cache_key"
NO_COMPANY_SCOPE = -1


class CompanyMiddleware:
    """
    Middleware to handle company-specific filtering for models.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def _get_company_id(self, request):
        """
        Retrieve the company ID from the request or session.
        """
        if getattr(request, "user", False) and not request.user.is_anonymous:
            user = request.user
            employee = getattr(user, "employee_get", None)
            own_company = getattr(
                getattr(employee, "employee_work_info", None),
                "company_id",
                None,
            )
            can_change_company = user.is_superuser or user.has_perm(
                "base.change_company"
            )
            selected_company = request.session.get("selected_company")
            if selected_company == "none":
                return None if can_change_company else NO_COMPANY_SCOPE
            if selected_company == "all":
                return None if can_change_company else own_company or NO_COMPANY_SCOPE
            if selected_company:
                selected = Company.objects.filter(id=selected_company).first()
                if selected and (
                    can_change_company
                    or selected.pk == getattr(own_company, "pk", None)
                ):
                    return selected
                return own_company or NO_COMPANY_SCOPE
            if can_change_company:
                return None
            return own_company or NO_COMPANY_SCOPE
        return None

    def _set_company_session(self, request, company_id):
        """
        Set the company session data based on the company ID.
        """
        user = getattr(request.user, "employee_get", None)
        can_change_company = request.user.is_superuser or request.user.has_perm(
            "base.change_company"
        )
        if user is None and not can_change_company:
            request.session["selected_company"] = "none"
            request.session["selected_company_instance"] = {
                "company": str(_("No company access")),
                "icon": AllCompany.Urls.url,
                "text": str(_("No company access")),
                "id": None,
            }
            return
        user_company_id = getattr(
            getattr(user, "employee_work_info", None), "company_id", None
        )
        if company_id == NO_COMPANY_SCOPE:
            request.session["selected_company"] = "none"
            request.session["selected_company_instance"] = {
                "company": str(_("No company access")),
                "icon": AllCompany.Urls.url,
                "text": str(_("No company access")),
                "id": None,
            }
            return
        if company_id and request.session.get("selected_company") != "all":
            if company_id == "all":
                text = str(_("All companies"))
            elif company_id == user_company_id:
                text = "My Company"
            else:
                text = "Other Company"

            request.session["selected_company"] = str(company_id.id)
            request.session["selected_company_instance"] = {
                "company": company_id.company,
                "icon": company_id.icon.url,
                "text": text,
                "id": company_id.id,
            }
        else:
            request.session["selected_company"] = "all"
            all_company = AllCompany()
            request.session["selected_company_instance"] = {
                "company": all_company.company,
                "icon": all_company.icon.url,
                "text": all_company.text,
                "id": all_company.id,
            }

    def _add_company_filter(self, model, company_id):
        """
        Add company filter to the model if applicable.
        """
        is_company_model = model in self._get_company_models()
        company_field = getattr(model, "company_id", None)
        is_horilla_manager = isinstance(model.objects, HorillaCompanyManager)
        related_company_field = getattr(model.objects, "related_company_field", None)

        if is_company_model:
            if company_field:
                model.add_to_class("company_filter", Q(company_id=company_id))
            elif is_horilla_manager and related_company_field:
                model.add_to_class(
                    "company_filter", Q(**{related_company_field: company_id})
                )
        else:
            if company_field:
                model.add_to_class(
                    "company_filter",
                    Q(company_id=company_id) | Q(company_id__isnull=True),
                )
            elif is_horilla_manager and related_company_field:
                model.add_to_class(
                    "company_filter",
                    Q(**{related_company_field: company_id})
                    | Q(**{f"{related_company_field}__isnull": True}),
                )

    def _get_company_models(self):
        """
        Retrieve the list of models that are company-specific.
        """
        company_models = cache.get(CACHE_KEY)

        if company_models is None:
            company_models = [
                Employee,
                ShiftRequest,
                WorkTypeRequest,
                DocumentRequest,
                DisciplinaryAction,
                EmployeeBankDetails,
                EmployeeWorkInformation,
            ]

            app_model_mappings = {
                "recruitment": ["recruitment", "candidate"],
                "leave": [
                    "leaverequest",
                    "restrictleave",
                    "availableleave",
                    "leaveallocationrequest",
                    "compensatoryleaverequest",
                ],
                "asset": ["assetassignment", "assetrequest"],
                "attendance": [
                    "attendance",
                    "attendanceactivity",
                    "attendanceovertime",
                    "workrecords",
                ],
                "payroll": [
                    "contract",
                    "loanaccount",
                    "payslip",
                    "reimbursement",
                ],
                "helpdesk": ["ticket"],
                "offboarding": ["offboarding"],
                "pms": ["employeeobjective"],
            }

            for app_label, models in app_model_mappings.items():
                if apps.is_installed(app_label):
                    company_models.extend(
                        [get_horilla_model_class(app_label, model) for model in models]
                    )

            cache.set(CACHE_KEY, company_models)

        return company_models

    def __call__(self, request):
        if getattr(request, "user", False) and not request.user.is_anonymous:
            company_id = self._get_company_id(request)
            self._set_company_session(request, company_id)

            app_models = [
                model for model in apps.get_models() if model._meta.app_label in APPS
            ]
            for model in app_models:
                self._add_company_filter(model, company_id)

        response = self.get_response(request)
        return response


class ForcePasswordChangeMiddleware:
    """
    Middleware to force password change for new employees.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        excluded_paths = ["/change-password", "/login", "/logout"]
        if request.path.rstrip("/") in excluded_paths:
            return self.get_response(request)

        if hasattr(request, "user") and request.user.is_authenticated:
            if getattr(request.user, "is_new_employee", True):
                return redirect("change-password")

        return self.get_response(request)


class TwoFactorAuthMiddleware:
    """
    Middleware to enforce two-factor authentication for specific users.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        excluded_paths = [
            "/change-password",
            "/login",
            "/logout",
            "/two-factor",
            "/send-otp",
        ]

        if request.path.rstrip("/") in excluded_paths:
            return self.get_response(request)

        if TWO_FACTORS_AUTHENTICATION:
            try:
                if ConfiguredEmailBackend().configuration is not None:
                    if hasattr(request, "user") and request.user.is_authenticated:
                        if not request.session.get("otp_code_verified", False):
                            return redirect("/two-factor")
                else:
                    return self.get_response(request)
            except Exception as e:
                return self.get_response(request)

        return self.get_response(request)
