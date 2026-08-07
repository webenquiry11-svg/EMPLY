from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.pagination import PageNumberPagination

from attendance.models import Attendance
from horilla_api.api_serializers.integration.serializers import WorkRadarAttendanceSerializer

class WorkRadarAttendanceAPIView(APIView):
    """
    API View for the WorkRadar Attendance Integration.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = WorkRadarAttendanceSerializer

    def get(self, request):
        """
        Returns all attendance records for the WorkRadar integration.
        """
        attendances = Attendance.objects.all()
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(attendances, request)
        if page is not None:
            serializer = self.serializer_class(page, many=True)
            return paginator.get_paginated_response(serializer.data)

        serializer = self.serializer_class(attendances, many=True)
        return Response(serializer.data)
