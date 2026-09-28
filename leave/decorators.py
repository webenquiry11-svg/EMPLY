"""
decorator functions for leave
"""

from django.contrib import messages
from django.shortcuts import redirect
from django.utils.translation import gettext_lazy as _

from horilla.methods import handle_no_permission
from leave.models import LeaveGeneralSetting

from .models import LeaveAllocationRequest

decorator_with_arguments = (
    lambda decorator: lambda *args, **kwargs: lambda func: decorator(
        func, *args, **kwargs
    )
)


@decorator_with_arguments
def leave_allocation_change_permission(function=None, *args, **kwargs):
    """Decorator to enforce permission for changing a leave allocation request."""

    def check_permission(
        request,
        req_id=None,
        *args,
        **kwargs,
    ):
        """
        This method is used to check the employee can change a leave allocation request or not
        """
        leave_allocation_request = LeaveAllocationRequest.objects.get(id=req_id)
        if (
            request.user.has_perm("leave.change_leaveallocationrequest")
            or request.user.employee_get
            == leave_allocation_request.employee_id.employee_work_info.reporting_manager_id
            or request.user.employee_get == leave_allocation_request.employee_id
        ):
            return function(request, *args, req_id=req_id, **kwargs)

        return handle_no_permission(request)

    return check_permission


@decorator_with_arguments
def leave_allocation_delete_permission(function=None, *args, **kwargs):
    """Decorator to enforce permission for deleting a leave allocation request."""

    def check_permission(
        request,
        req_id=None,
        *args,
        **kwargs,
    ):
        """
        This method is used to check the employee can delete a leave allocation request or not
        """
        try:
            leave_allocation_request = LeaveAllocationRequest.objects.get(id=req_id)
            if (
                request.user.has_perm("leave.delete_leaveallocationrequest")
                or request.user.employee_get
                == leave_allocation_request.employee_id.employee_work_info.reporting_manager_id
                or request.user.employee_get == leave_allocation_request.employee_id
            ):
                return function(request, *args, req_id=req_id, **kwargs)
            return handle_no_permission(request)
        except (LeaveAllocationRequest.DoesNotExist, OverflowError, ValueError):
            messages.error(request, _("Leave allocation request not found"))
            return redirect("/leave/leave-allocation-request-view/")

    return check_permission


@decorator_with_arguments
def leave_allocation_reject_permission(function=None, *args, **kwargs):
    """Decorator to enforce permission for rejecting a leave allocation request."""

    def check_permission(
        request,
        req_id=None,
        *args,
        **kwargs,
    ):
        """
        This method is used to check the employee can reject a leave allocation request or not
        """
        try:
            leave_allocation_request = LeaveAllocationRequest.objects.get(id=req_id)
            if (
                request.user.has_perm("leave.delete_leaveallocationrequest")
                or request.user.employee_get
                == leave_allocation_request.employee_id.employee_work_info.reporting_manager_id
            ):
                return function(request, *args, req_id=req_id, **kwargs)
            return handle_no_permission(request)
        except (LeaveAllocationRequest.DoesNotExist, OverflowError, ValueError):
            messages.error(request, _("Leave allocation request not found"))
            return redirect("/leave/leave-allocation-request-view/")

    return check_permission


@decorator_with_arguments
def is_compensatory_leave_enabled(func=None, *args, **kwargs):
    """Decorator to ensure compensatory leave is enabled before running the view."""

    def function(request, *args, **kwargs):
        """
        This function check whether the compensatory leave feature is enabled
        """
        if (
            LeaveGeneralSetting.objects.exists()
            and LeaveGeneralSetting.objects.all().first().compensatory_leave
        ):
            return func(request, *args, **kwargs)
        return handle_no_permission(
            request, message=_("Compensatory leave is not enabled.")
        )

    return function


def block_notice_period(view_func):
    """Decorator to block access to certain leave views for employees on notice period.

    Behavior:
    - If the logged-in user has an Employee (request.user.employee_get) with
      notice_period == True, and the user does NOT hold any leave.* permission
      nor is superuser, then the view is blocked and a consistent notice message
      is shown via handle_no_permission.
    - Users with any leave.* permission or superusers bypass this check so that
      admins/managers keep their existing access.

    This decorator is intended to be added to employee self-service leave views
    (creation, request lists, dashboard, allocation requests, compensatory
    requests, etc.). Company Leaves and Holidays views should NOT be decorated
    so they remain accessible.
    """

    message = _(
        "You are currently on notice period. Leave access is restricted during your notice period."
    )

    def _wrapped_view(request, *args, **kwargs):
        try:
            user = request.user
        except Exception:
            # No user available; let auth decorators handle this
            return view_func(request, *args, **kwargs)

        emp = getattr(user, "employee_get", None)
        if emp and getattr(emp, "notice_period", False):
            # Allow superusers
            if getattr(user, "is_superuser", False):
                return view_func(request, *args, **kwargs)

            # Allow users who hold any leave.* permission (managers/admins)
            try:
                perms = user.get_all_permissions()
                for p in perms:
                    if p.startswith("leave."):
                        return view_func(request, *args, **kwargs)
            except Exception:
                # If permission inspection fails, fall through to block
                pass

            # Block access
            return handle_no_permission(request, message=message)

        return view_func(request, *args, **kwargs)

    return _wrapped_view
