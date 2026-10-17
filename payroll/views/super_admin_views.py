"""Superuser-only payroll and organization administration."""

from django import forms
from django.contrib import messages
from django.contrib.admin.models import ADDITION, CHANGE, LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from django.contrib.auth.models import Group
from django.contrib.contenttypes.models import ContentType
from django.core.paginator import Paginator
from django.db.models import Sum
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _

from base.forms import CompanyForm
from base.models import Company
from payroll.forms.component_forms import PayrollSettingsForm
from payroll.models.models import Payslip, PayrollApprovalEvent, PayrollRun
from payroll.models.tax_models import PayrollSettings

User = get_user_model()


class SuperAdminUserCreationForm(UserCreationForm):
    email = forms.EmailField(required=False)
    first_name = forms.CharField(required=False)
    last_name = forms.CharField(required=False)
    is_active = forms.BooleanField(required=False, initial=True)
    is_staff = forms.BooleanField(required=False)
    is_superuser = forms.BooleanField(required=False)
    groups = forms.ModelMultipleChoiceField(
        queryset=Group.objects.all(), required=False
    )
    user_permissions = forms.ModelMultipleChoiceField(
        queryset=None, required=False
    )

    class Meta(UserCreationForm.Meta):
        model = User
        fields = (
            "username",
            "email",
            "first_name",
            "last_name",
            "is_active",
            "is_staff",
            "is_superuser",
            "groups",
            "user_permissions",
        )

    def __init__(self, *args, **kwargs):
        from django.contrib.auth.models import Permission

        super().__init__(*args, **kwargs)
        self.fields["user_permissions"].queryset = Permission.objects.select_related(
            "content_type"
        ).order_by("content_type__app_label", "codename")


class SuperAdminGroupForm(forms.ModelForm):
    class Meta:
        model = Group
        fields = ("name", "permissions")
        widgets = {"permissions": forms.SelectMultiple(attrs={"size": 14})}


def _superuser_only(request):
    return request.user.is_authenticated and request.user.is_superuser


def _record_admin_change(request, instance, action_flag, message):
    content_type = ContentType.objects.get_for_model(instance)
    LogEntry.objects.log_action(
        user_id=request.user.pk,
        content_type_id=content_type.pk,
        object_id=str(instance.pk),
        object_repr=str(instance)[:200],
        action_flag=action_flag,
        change_message=message,
    )


@login_required
def super_admin_dashboard(request):
    if not _superuser_only(request):
        return HttpResponseForbidden(_("Super Admin access is required."))
    paid = Payslip._base_manager.filter(status="paid")
    return render(
        request,
        "payroll/super_admin_dashboard.html",
        {
            "company_count": Company.objects.count(),
            "user_count": User._default_manager.count(),
            "paid_count": paid.count(),
            "paid_total": paid.aggregate(total=Sum("net_pay"))["total"] or 0,
            "runs": PayrollRun._base_manager.select_related("company")[:12],
            "error_runs": PayrollRun._base_manager.filter(status="rejected")
            .select_related("company")[:10],
            "audit_events": PayrollApprovalEvent.objects.select_related(
                "payroll_run", "actor"
            )[:12],
        },
    )


@login_required
def super_admin_users(request):
    if not _superuser_only(request):
        return HttpResponseForbidden(_("Super Admin access is required."))
    user_id = request.GET.get("id") or request.POST.get("user_id")
    instance = (
        get_object_or_404(User._default_manager, pk=user_id) if user_id else None
    )
    if request.method == "POST":
        form_class = UserChangeForm if instance else SuperAdminUserCreationForm
        form = form_class(request.POST, instance=instance) if instance else form_class(request.POST)
        if form.is_valid():
            user = form.save()
            _record_admin_change(
                request,
                user,
                CHANGE if instance else ADDITION,
                _("Updated account and authorization settings.")
                if instance
                else _("Created account and assigned authorization settings."),
            )
            messages.success(request, _("Account saved."))
            return redirect("super-admin-users")
    else:
        form = UserChangeForm(instance=instance) if instance else SuperAdminUserCreationForm()
    users = Paginator(User._default_manager.order_by("username"), 30).get_page(
        request.GET.get("page")
    )
    return render(
        request,
        "payroll/super_admin_management.html",
        {
            "title": _("Admin accounts"),
            "form": form,
            "objects": users,
            "object_kind": "user",
        },
    )


@login_required
def super_admin_groups(request):
    if not _superuser_only(request):
        return HttpResponseForbidden(_("Super Admin access is required."))
    group_id = request.GET.get("id") or request.POST.get("group_id")
    instance = get_object_or_404(Group, pk=group_id) if group_id else None
    form = SuperAdminGroupForm(
        request.POST or None,
        instance=instance,
    )
    if request.method == "POST" and form.is_valid():
        group = form.save()
        _record_admin_change(
            request,
            group,
            CHANGE if instance else ADDITION,
            _("Updated group permissions.") if instance else _("Created group and assigned permissions."),
        )
        messages.success(request, _("Group and permissions saved."))
        return redirect("super-admin-groups")
    groups = Paginator(
        Group.objects.prefetch_related("permissions").order_by("name"), 30
    ).get_page(request.GET.get("page"))
    return render(
        request,
        "payroll/super_admin_management.html",
        {
            "title": _("Groups and permissions"),
            "form": form,
            "objects": groups,
            "object_kind": "group",
        },
    )


@login_required
def super_admin_companies(request):
    if not _superuser_only(request):
        return HttpResponseForbidden(_("Super Admin access is required."))
    company_id = request.GET.get("id") or request.POST.get("company_id")
    instance = get_object_or_404(Company, pk=company_id) if company_id else None
    form = CompanyForm(
        request.POST or None,
        request.FILES or None,
        instance=instance,
    )
    if request.method == "POST" and form.is_valid():
        company = form.save()
        _record_admin_change(
            request,
            company,
            CHANGE if instance else ADDITION,
            _("Updated company settings.") if instance else _("Created company."),
        )
        messages.success(request, _("Company settings saved."))
        return redirect("super-admin-companies")
    companies = Paginator(Company.objects.order_by("company"), 30).get_page(
        request.GET.get("page")
    )
    return render(
        request,
        "payroll/super_admin_management.html",
        {
            "title": _("Company settings"),
            "form": form,
            "objects": companies,
            "object_kind": "company",
        },
    )


@login_required
def super_admin_payroll_settings(request):
    if not _superuser_only(request):
        return HttpResponseForbidden(_("Super Admin access is required."))
    settings_id = request.GET.get("id") or request.POST.get("settings_id")
    instance = (
        get_object_or_404(PayrollSettings, pk=settings_id) if settings_id else None
    )
    form = PayrollSettingsForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        settings_obj = form.save()
        _record_admin_change(
            request,
            settings_obj,
            CHANGE if instance else ADDITION,
            _("Updated payroll configuration.")
            if instance
            else _("Created payroll configuration."),
        )
        messages.success(request, _("Payroll configuration saved."))
        return redirect("super-admin-payroll-settings")
    settings_rows = Paginator(
        PayrollSettings.objects.select_related("company_id").order_by(
            "company_id__company"
        ),
        30,
    ).get_page(request.GET.get("page"))
    return render(
        request,
        "payroll/super_admin_management.html",
        {
            "title": _("Payroll configuration"),
            "form": form,
            "objects": settings_rows,
            "object_kind": "settings",
        },
    )


@login_required
def super_admin_audit(request):
    if not _superuser_only(request):
        return HttpResponseForbidden(_("Super Admin access is required."))
    log_entries = LogEntry.objects.select_related("user", "content_type").order_by(
        "-action_time"
    )
    events = PayrollApprovalEvent.objects.select_related(
        "actor", "payroll_run", "payroll_run__company"
    )
    return render(
        request,
        "payroll/super_admin_audit.html",
        {
            "log_entries": Paginator(log_entries, 50).get_page(
                request.GET.get("page")
            ),
            "events": events[:100],
        },
    )
