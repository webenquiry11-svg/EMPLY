from django.urls import path
from horilla_api.api_views.integration.views import WorkRadarAttendanceAPIView

urlpatterns = [
    path('attendance/', WorkRadarAttendanceAPIView.as_view(), name='workradar-attendance'),
]
