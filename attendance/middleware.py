"""
Attendance middleware.

The automatic punch-out logic is intentionally not executed during
HTTP requests because it can perform expensive database queries and
block the web request.
"""

from django.utils.deprecation import MiddlewareMixin


class AttendanceMiddleware(MiddlewareMixin):
    """
    Middleware for attendance-related request processing.

    Automatic punch-out should be handled by a background scheduler/job,
    not during every HTTP request.
    """

    def process_request(self, request):
        """
        Do not run attendance auto punch-out logic here.

        Returning None allows Django to continue processing the request
        immediately.
        """
        return None