import os
from datetime import datetime

from django import apps
from django.db import models
from django.db.models.signals import post_delete, post_save
from django.forms import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from base.horilla_company_manager import HorillaCompanyManager
from base.models import Company, Department, JobPosition, Tags
from employee.models import Employee
from horilla import horilla_middlewares
from horilla.models import HorillaModel, upload_path
from horilla_audit.methods import get_diff
from horilla_audit.models import HorillaAuditInfo, HorillaAuditLog

PRIORITY = [
    ("low", _("Low")),
    ("medium", _("Medium")),
    ("high", _("High")),
]
MANAGER_TYPES = [
    ("department", _("Department")),
    ("job_position", _("Job Position")),
    ("individual", _("Individual")),
]

TICKET_TYPES = [
    ("suggestion", _("Suggestion")),
    ("complaint", _("Complaint")),
    ("service_request", _("Service Request")),
    ("meeting_request", _("Meeting Request")),
    ("anounymous_complaint", _("Anonymous Complaint")),
    ("others", _("Others")),
]

TICKET_STATUS = [
    ("new", _("New")),
    ("accepted", _("Accepted")),
    ("rejected", _("Rejected")),
    ("assigned", _("Assigned")),
    ("in_progress", _("In Progress")),
    ("on_hold", _("On Hold")),
    ("resolved", _("Resolved")),
    ("closed", _("Closed")),
    ("canceled", _("Canceled")),
]


class DepartmentManager(HorillaModel):
    manager = models.ForeignKey(
        Employee,
        verbose_name=_("Manager"),
        related_name="dep_manager",
        on_delete=models.CASCADE,
    )
    department = models.ForeignKey(
        Department,
        verbose_name=_("Department"),
        related_name="dept_manager",
        on_delete=models.CASCADE,
    )
    company_id = models.ForeignKey(
        Company, null=True, editable=False, on_delete=models.PROTECT
    )
    objects = HorillaCompanyManager("manager__employee_work_info__company_id")

    class Meta:
        unique_together = ("department", "manager")
        verbose_name = _("Department Manager")
        verbose_name_plural = _("Department Managers")

    def clean(self, *args, **kwargs):
        super().clean(*args, **kwargs)
        if not self.manager.get_department() == self.department:
            raise ValidationError(_(f"This employee is not from {self.department} ."))


class TicketType(HorillaModel):
    title = models.CharField(max_length=100, unique=True, verbose_name=_("Title"))
    type = models.CharField(choices=TICKET_TYPES, max_length=50, verbose_name=_("Type"))
    prefix = models.CharField(max_length=3, unique=True, verbose_name=_("Prefix"))
    company_id = models.ForeignKey(
        Company, null=True, editable=False, on_delete=models.PROTECT
    )
    objects = HorillaCompanyManager(related_company_field="company_id")

    def __str__(self):
        return self.title

    class Meta:
        verbose_name = _("Ticket Type")
        verbose_name_plural = _("Ticket Types")


class Ticket(HorillaModel):

    title = models.CharField(max_length=50)
    employee_id = models.ForeignKey(
        Employee,
        on_delete=models.PROTECT,
        related_name="ticket",
        verbose_name=_("Owner"),
    )
    ticket_type = models.ForeignKey(
        TicketType,
        on_delete=models.PROTECT,
        verbose_name=_("Ticket Type"),
    )
    description = models.TextField(max_length=255)
    priority = models.CharField(choices=PRIORITY, max_length=100, default="low")
    complaint = models.TextField(
        max_length=1000,
        null=True,
        blank=True,
        verbose_name=_("Complaint"),
    )
    support_image = models.ImageField(
        upload_to=upload_path,
        null=True,
        blank=True,
        verbose_name=_("Support Image"),
    )
    created_date = models.DateField(auto_now_add=True)
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    resolved_date = models.DateField(blank=True, null=True)
    closed_at = models.DateTimeField(blank=True, null=True)
    resolver = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="support_ticket_resolver",
        verbose_name=_("Resolver"),
    )
    closed = models.BooleanField(default=False, verbose_name=_("Closed"))
    is_support_ticket = models.BooleanField(default=False, verbose_name=_("Support Ticket"))
    assigning_type = models.CharField(
        choices=MANAGER_TYPES, max_length=100, verbose_name=_("Assigning Type")
    )
    raised_on = models.CharField(max_length=100, verbose_name=_("Forward To"))
    assigned_to = models.ManyToManyField(
        Employee, blank=True, related_name="ticket_assigned_to"
    )
    deadline = models.DateField(null=True, blank=True, verbose_name=_("Deadline"))
    tags = models.ManyToManyField(Tags, blank=True, related_name="ticket_tags")
    status = models.CharField(choices=TICKET_STATUS, default="new", max_length=50)
    history = HorillaAuditLog(
        related_name="history_set",
        bases=[
            HorillaAuditInfo,
        ],
    )
    objects = HorillaCompanyManager(
        related_company_field="employee_id__employee_work_info__company_id"
    )

    class Meta:
        ordering = ["-created_date"]
        verbose_name = _("Ticket")
        verbose_name_plural = _("Tickets")

    def get_raised_on(self):
        obj_id = self.raised_on
        if self.assigning_type == "department":
            raised_on = Department.objects.get(id=obj_id).department
        elif self.assigning_type == "job_position":
            raised_on = JobPosition.objects.get(id=obj_id).job_position
        elif self.assigning_type == "individual":
            raised_on = Employee.objects.get(id=obj_id).get_full_name()
        return raised_on

    def get_raised_on_object(self):
        obj_id = self.raised_on
        if self.assigning_type == "department":
            raised_on = Department.objects.get(id=obj_id)
        elif self.assigning_type == "job_position":
            raised_on = JobPosition.objects.get(id=obj_id)
        elif self.assigning_type == "individual":
            raised_on = Employee.objects.get(id=obj_id)
        return raised_on

    def __str__(self):
        return self.title

    def get_support_complaint(self):
        return self.complaint or self.description or ""

    def get_elapsed_time(self):
        if not self.created_at:
            return ""
        delta = timezone.now() - self.created_at
        total_seconds = max(int(delta.total_seconds()), 0)
        if total_seconds < 60:
            return f"{total_seconds}s ago"
        minutes, seconds = divmod(total_seconds, 60)
        if minutes < 60:
            return f"{minutes}m ago"
        hours, minutes = divmod(minutes, 60)
        if hours < 24:
            return f"{hours}h ago"
        days, hours = divmod(hours, 24)
        return f"{days}d ago"

    def can_user_access(self, employee):
        if not employee:
            return False
        if employee.employee_user_id and employee.employee_user_id.is_superuser:
            return True
        if employee == self.employee_id:
            return True
        if self.resolver and employee == self.resolver:
            return True
        if employee in self.assigned_to.all():
            return True
        return False

    def tracking(self):
        """
        This method is used to return the tracked history of the instance
        """
        return get_diff(self)


class ClaimRequest(HorillaModel):
    ticket_id = models.ForeignKey(
        Ticket,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    employee_id = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    is_approved = models.BooleanField(default=False)
    is_rejected = models.BooleanField(default=False)

    class Meta:
        unique_together = ("ticket_id", "employee_id")

    def __str__(self) -> str:
        return f"{self.ticket_id}|{self.employee_id}"

    def clean(self, *args, **kwargs):
        super().clean(*args, **kwargs)
        if not self.ticket_id:
            raise ValidationError({"ticket_id": _("This field is required.")})
        if not self.employee_id:
            raise ValidationError({"employee_id": _("This field is required.")})


class Comment(HorillaModel):
    comment = models.TextField(null=True, blank=True)
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="comment")
    employee_id = models.ForeignKey(
        Employee, on_delete=models.DO_NOTHING, related_name="employee_comment"
    )
    date = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.comment


class Attachment(HorillaModel):
    file = models.FileField(upload_to=upload_path)
    description = models.CharField(max_length=100, blank=True, null=True)
    format = models.CharField(max_length=50, blank=True, null=True)
    ticket = models.ForeignKey(
        Ticket,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="ticket_attachment",
    )
    comment = models.ForeignKey(
        Comment,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="comment_attachment",
    )

    def get_file_format(self):
        image_format = [".jpg", ".jpeg", ".png", ".svg"]
        audio_format = [".m4a", ".mp3"]
        file_extension = os.path.splitext(self.file.url)[1].lower()
        if file_extension in audio_format:
            self.format = "audio"
        elif file_extension in image_format:
            self.format = "image"
        else:
            self.format = "file"

    def save(self, *args, **kwargs):
        self.get_file_format()

        super().save(self, *args, **kwargs)

    def __str__(self):
        return os.path.basename(self.file.name)


class FAQCategory(HorillaModel):
    title = models.CharField(max_length=30, verbose_name=_("Title"))
    description = models.TextField(
        blank=True, null=True, max_length=255, verbose_name=_("Description")
    )
    company_id = models.ForeignKey(
        Company,
        null=True,
        blank=True,
        editable=False,
        verbose_name=_("Company"),
        on_delete=models.CASCADE,
    )
    objects = HorillaCompanyManager()

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        request = getattr(horilla_middlewares._thread_locals, "request", None)
        selected_company = request.session.get("selected_company")
        if (
            not self.id
            and not self.company_id
            and selected_company
            and selected_company != "all"
        ):
            self.company_id = Company.find(selected_company)

        super().save()

    class Meta:
        verbose_name = _("FAQ Category")
        verbose_name_plural = _("FAQ Categories")


class FAQ(HorillaModel):
    question = models.CharField(max_length=255)
    answer = models.TextField()
    tags = models.ManyToManyField(Tags, blank=True)
    category = models.ForeignKey(FAQCategory, on_delete=models.PROTECT)
    company_id = models.ForeignKey(
        Company, null=True, editable=False, on_delete=models.PROTECT
    )
    objects = HorillaCompanyManager()

    def __str__(self):
        return self.question

    def save(self, *args, **kwargs):
        request = getattr(horilla_middlewares._thread_locals, "request", None)
        selected_company = request.session.get("selected_company")
        if (
            not self.id
            and not self.company_id
            and selected_company
            and selected_company != "all"
        ):
            self.company_id = Company.find(selected_company)

        super().save()

    class Meta:
        verbose_name = _("FAQ")
        verbose_name_plural = _("FAQs")
