from rest_framework import serializers
from attendance.models import Attendance

class WorkRadarAttendanceSerializer(serializers.ModelSerializer):
    """
    Serializer for the WorkRadar Attendance Integration.
    """
    employee_name = serializers.CharField(source='employee_id.get_full_name', read_only=True)
    employee_id = serializers.CharField(source='employee_id.badge_id', read_only=True)
    attendance_date = serializers.DateField(read_only=True)
    check_in = serializers.TimeField(source='attendance_clock_in', read_only=True)
    check_out = serializers.TimeField(source='attendance_clock_out', read_only=True)

    class Meta:
        model = Attendance
        fields = [
            'employee_name',
            'employee_id',
            'attendance_date',
            'check_in',
            'check_out',
        ]
