import gettext
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import permission_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import render
from django.utils.decorators import method_decorator
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from base.backends import ConfiguredEmailBackend
from payroll.filters import (
    AllowanceFilter,
    ContractFilter,
    DeductionFilter,
    PayslipFilter,
)
from payroll.models.models import (
    Allowance,
    Contract,
    Deduction,
    FilingStatus,
    LoanAccount,
    Payslip,
    Reimbursement,
)
from payroll.models.tax_models import TaxBracket
from payroll.threadings.mail import MailSendThread
from payroll.views.views import payslip_pdf

from ...api_methods.base.methods import groupby_queryset
from ...api_serializers.payroll.serializers import (
    AllowanceSerializer,
    ContractSerializer,
    DeductionSerializer,
    LoanAccountSerializer,
    PayslipSerializer,
    ReimbursementSerializer,
    TaxBracketSerializer,
)


class PayslipView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, id=None):
        if id:
            payslips = Payslip.objects.all()
            if not request.user.has_perm("payroll.view_payslip"):
                payslips = payslips.filter(employee_id__employee_user_id=request.user)
            payslip = payslips.filter(id=id).first()
            if payslip is None:
                return Response({"detail": "Payslip not found."}, status=404)
            serializer = PayslipSerializer(payslip)
            return Response(serializer.data, status=200)
        if request.user.has_perm("payroll.view_payslip"):
            payslips = Payslip.objects.all()
        else:
            payslips = Payslip.objects.filter(
                employee_id__employee_user_id=request.user
            )

        payslip_filter_queryset = PayslipFilter(request.GET, payslips).qs
        # groupby workflow
        field_name = request.GET.get("groupby_field", None)
        if field_name:
            url = request.build_absolute_uri()
            return groupby_queryset(request, url, field_name, payslip_filter_queryset)
        pagination = PageNumberPagination()
        page = pagination.paginate_queryset(payslip_filter_queryset, request)
        serializer = PayslipSerializer(page, many=True)
        return pagination.get_paginated_response(serializer.data)


class PayslipDownloadView(APIView):

    permission_classes = [IsAuthenticated]

    def get(self, request, id):
        payslips = Payslip.objects.all()
        if not request.user.has_perm("payroll.view_payslip"):
            payslips = payslips.filter(employee_id__employee_user_id=request.user)
        if not payslips.filter(id=id).exists():
            return Response(
                {"detail": "Payslip not found or you do not have permission."},
                status=404,
            )
        return payslip_pdf(request, id)


class PayslipSendMailView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not request.user.has_perm("payroll.add_payslip"):
            return Response({"detail": "You do not have permission."}, status=403)
        email_backend = ConfiguredEmailBackend()
        if not getattr(
            email_backend, "dynamic_username_with_display_name", None
        ) or not len(email_backend.dynamic_username_with_display_name):
            return Response({"error": "Email server is not configured"}, status=400)

        payslip_ids = request.data.get("id", [])
        if (
            not isinstance(payslip_ids, list)
            or not payslip_ids
            or any(
                isinstance(pk, bool)
                or not str(pk).isdigit()
                or int(pk) <= 0
                for pk in payslip_ids
            )
            or len(set(map(str, payslip_ids))) != len(payslip_ids)
        ):
            return Response({"detail": "Invalid payslip selection."}, status=400)
        payslip_ids = [int(pk) for pk in payslip_ids]
        payslips = Payslip.objects.filter(id__in=payslip_ids)
        if payslips.count() != len(payslip_ids):
            return Response(
                {"detail": "One or more payslips were not found."}, status=404
            )
        result_dict = defaultdict(
            lambda: {"employee_id": None, "instances": [], "count": 0}
        )

        for payslip in payslips:
            employee_id = payslip.employee_id
            result_dict[employee_id]["employee_id"] = employee_id
            result_dict[employee_id]["instances"].append(payslip)
            result_dict[employee_id]["count"] += 1
        mail_thread = MailSendThread(request, result_dict=result_dict, ids=payslip_ids)
        mail_thread.start()
        return Response({"status": "success"}, status=200)


class ContractView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, id=None):
        if id:
            if request.user.has_perm("payroll.view_contract"):
                contract = Contract.objects.filter(id=id).first()
            else:
                contract = Contract.objects.filter(
                    id=id, employee_id=request.user.employee_get
                ).first()
            if not contract:
                return Response(status=404)
            serializer = ContractSerializer(contract)
            return Response(serializer.data, status=200)
        if request.user.has_perm("payroll.view_contract"):
            contracts = Contract.objects.all()
        else:
            contracts = Contract.objects.filter(employee_id=request.user.employee_get)
        filter_queryset = ContractFilter(request.GET, contracts).qs
        # groupby workflow
        field_name = request.GET.get("groupby_field", None)
        if field_name:
            url = request.build_absolute_uri()
            return groupby_queryset(request, url, field_name, filter_queryset)
        pagination = PageNumberPagination()
        page = pagination.paginate_queryset(filter_queryset, request)
        serializer = ContractSerializer(page, many=True)
        return pagination.get_paginated_response(serializer.data)

    @method_decorator(permission_required("payroll.add_contract"))
    def post(self, request):
        serializer = ContractSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=200)
        return Response(serializer.errors, status=400)

    @method_decorator(permission_required("payroll.change_contract"))
    def put(self, request, pk):
        contract = Contract.objects.get(id=pk)
        serializer = ContractSerializer(instance=contract, data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=200)
        return Response(serializer.errors, status=400)

    @method_decorator(permission_required("payroll.delete_contract"))
    def delete(self, request, pk):
        contract = Contract.objects.get(id=pk)
        contract.delete()
        return Response({"status": "deleted"}, status=200)


class AllowanceView(APIView):
    permission_classes = [IsAuthenticated]

    @method_decorator(permission_required("payroll.view_allowance"))
    def get(self, request, pk=None):
        if pk:
            allowance = Allowance.objects.get(id=pk)
            serializer = AllowanceSerializer(instance=allowance)
            return Response(serializer.data, status=200)
        allowance = Allowance.objects.all()
        filter_queryset = AllowanceFilter(request.GET, allowance).qs
        pagination = PageNumberPagination()
        page = pagination.paginate_queryset(filter_queryset, request)
        serializer = AllowanceSerializer(page, many=True)
        return pagination.get_paginated_response(serializer.data)

    @method_decorator(permission_required("payroll.add_allowance"))
    def post(self, request):
        serializer = AllowanceSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=200)
        return Response(serializer.errors, status=400)

    @method_decorator(permission_required("payroll.change_allowance"))
    def put(self, request, pk):
        contract = Allowance.objects.get(id=pk)
        serializer = AllowanceSerializer(instance=contract, data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=200)
        return Response(serializer.errors, status=400)

    @method_decorator(permission_required("payroll.delete_allowance"))
    def delete(self, request, pk):
        contract = Allowance.objects.get(id=pk)
        contract.delete()
        return Response({"status": "deleted"}, status=200)


class DeductionView(APIView):
    permission_classes = [IsAuthenticated]

    @method_decorator(permission_required("payroll.view_deduction"))
    def get(self, request, pk=None):
        if pk:
            deduction = Deduction.objects.get(id=pk)
            serializer = DeductionSerializer(instance=deduction)
            return Response(serializer.data, status=200)
        deduction = Deduction.objects.all()
        filter_queryset = DeductionFilter(request.GET, deduction).qs
        pagination = PageNumberPagination()
        page = pagination.paginate_queryset(filter_queryset, request)
        serializer = DeductionSerializer(page, many=True)
        return pagination.get_paginated_response(serializer.data)

    @method_decorator(permission_required("payroll.add_deduction"))
    def post(self, request):
        serializer = DeductionSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=200)
        return Response(serializer.errors, status=400)

    @method_decorator(permission_required("payroll.change_deduction"))
    def put(self, request, pk):
        contract = Deduction.objects.get(id=pk)
        serializer = DeductionSerializer(instance=contract, data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=200)
        return Response(serializer.errors, status=400)

    @method_decorator(permission_required("payroll.delete_deduction"))
    def delete(self, request, pk):
        contract = Deduction.objects.get(id=pk)
        contract.delete()
        return Response({"status": "deleted"}, status=200)


class LoanAccountView(APIView):
    permission_classes = [IsAuthenticated]

    @method_decorator(permission_required("payroll.add_loanaccount"))
    def post(self, request):
        serializer = LoanAccountSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=200)
        return Response(serializer.errors, status=400)

    @method_decorator(permission_required("payroll.view_loanaccount"))
    def get(self, request, pk=None):
        if pk:
            loan_account = LoanAccount.objects.get(id=pk)
            serializer = LoanAccountSerializer(instance=loan_account)
            return Response(serializer.data, status=200)
        loan_accounts = LoanAccount.objects.all()
        pagination = PageNumberPagination()
        page = pagination.paginate_queryset(loan_accounts, request)
        serializer = LoanAccountSerializer(page, many=True)
        return pagination.get_paginated_response(serializer.data)

    @method_decorator(permission_required("payroll.change_loanaccount"))
    def put(self, request, pk):
        loan_account = LoanAccount.objects.get(id=pk)
        serializer = LoanAccountSerializer(loan_account, data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=200)
        return Response(serializer.errors, status=400)

    @method_decorator(permission_required("payroll.delete_loanaccount"))
    def delete(self, request, pk):
        loan_account = LoanAccount.objects.get(id=pk)
        loan_account.delete()
        return Response(status=200)


class ReimbursementView(APIView):
    serializer_class = ReimbursementSerializer
    permission_classes = [IsAuthenticated]

    def get(self, request, pk=None):
        reimbursements = Reimbursement.objects.all()
        if not request.user.has_perm("payroll.view_reimbursement"):
            reimbursements = reimbursements.filter(
                employee_id__employee_user_id=request.user
            )
        if pk:
            reimbursement = reimbursements.filter(id=pk).first()
            if reimbursement is None:
                return Response({"detail": "Reimbursement not found."}, status=404)
            serializer = self.serializer_class(reimbursement)
            return Response(serializer.data, status=200)
        pagination = PageNumberPagination()
        page = pagination.paginate_queryset(reimbursements, request)
        serializer = self.serializer_class(page, many=True)
        return pagination.get_paginated_response(serializer.data)

    def post(self, request):
        user_employee = getattr(request.user, "employee_get", None)
        if not request.user.has_perm("payroll.change_reimbursement") and not user_employee:
            return Response(
                {"detail": "An employee profile is required to submit a reimbursement."},
                status=403,
            )
        serializer = self.serializer_class(
            data=request.data, context={"request": request}
        )
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=200)
        return Response(serializer.errors, status=400)

    def put(self, request, pk):
        if not request.user.has_perm("payroll.change_reimbursement"):
            return Response({"detail": "You do not have permission."}, status=403)
        reimbursement = Reimbursement.objects.filter(id=pk).first()
        if reimbursement is None:
            return Response({"detail": "Reimbursement not found."}, status=404)
        serializer = self.serializer_class(
            instance=reimbursement,
            data=request.data,
            context={"request": request},
        )
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=200)
        return Response(serializer.errors, status=400)

    def delete(self, request, pk):
        if not request.user.has_perm("payroll.delete_reimbursement"):
            return Response({"detail": "You do not have permission."}, status=403)
        reimbursement = Reimbursement.objects.filter(id=pk).first()
        if reimbursement is None:
            return Response({"detail": "Reimbursement not found."}, status=404)
        reimbursement.delete()
        return Response(status=200)


class ReimbusementApproveRejectView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        try:
            with transaction.atomic():
                reimbursement = (
                    Reimbursement.objects.select_for_update().filter(id=pk).first()
                )
                if reimbursement is None:
                    return Response({"detail": "Reimbursement not found."}, status=404)
                user_employee = getattr(request.user, "employee_get", None)
                owner = reimbursement.employee_id
                work_info = getattr(owner, "employee_work_info", None)
                reporting_manager = (
                    getattr(work_info, "reporting_manager_id", None)
                    if work_info
                    else None
                )
                is_manager = (
                    user_employee is not None and reporting_manager == user_employee
                )
                has_permission = request.user.has_perm(
                    "payroll.change_reimbursement"
                )

                if not (is_manager or has_permission):
                    return Response(
                        {
                            "error": "You do not have permission to approve/reject this reimbursement."
                        },
                        status=403,
                    )

                new_status = request.data.get("status")
                if new_status not in {"approved", "rejected"}:
                    return Response(
                        {"status": ["Choose either approved or rejected."]},
                        status=400,
                    )
                if reimbursement.status != "requested":
                    return Response(
                        {"status": ["Only requested reimbursements can be reviewed."]},
                        status=400,
                    )
                if "amount" in request.data:
                    try:
                        amount = Decimal(str(request.data["amount"]))
                    except (InvalidOperation, TypeError, ValueError):
                        return Response(
                            {"amount": ["Enter a valid amount."]}, status=400
                        )
                    if not amount.is_finite() or amount < 0:
                        return Response(
                            {
                                "amount": [
                                    "Amount must be a finite, non-negative number."
                                ]
                            },
                            status=400,
                        )
                    reimbursement.amount = float(amount)
                reimbursement.status = new_status
                reimbursement.approved_by = user_employee
                reimbursement.save()
        except ValidationError as exc:
            return Response({"detail": exc.messages}, status=400)
        return Response({"status": reimbursement.status}, status=200)


class TaxBracketView(APIView):
    permission_classes = [IsAuthenticated]

    def _has_permission(self, request, action):
        return request.user.has_perm(f"payroll.{action}_taxbracket")

    def _queryset(self):
        return TaxBracket.objects.filter(
            filing_status_id__in=FilingStatus.objects.all()
        )

    def get(self, request, pk=None):
        if not self._has_permission(request, "view"):
            return Response({"detail": "You do not have permission."}, status=403)
        if pk:
            tax_bracket = self._queryset().filter(id=pk).first()
            if tax_bracket is None:
                return Response({"detail": "Tax bracket not found."}, status=404)
            serializer = TaxBracketSerializer(tax_bracket)
            return Response(serializer.data, status=200)
        tax_brackets = self._queryset()
        serializer = TaxBracketSerializer(instance=tax_brackets, many=True)
        return Response(serializer.data, status=200)

    def post(self, request):
        if not self._has_permission(request, "add"):
            return Response({"detail": "You do not have permission."}, status=403)
        serializer = TaxBracketSerializer(
            data=request.data, context={"request": request}
        )
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=200)
        return Response(serializer.errors, status=400)

    def put(self, request, pk):
        if not self._has_permission(request, "change"):
            return Response({"detail": "You do not have permission."}, status=403)
        tax_bracket = self._queryset().filter(id=pk).first()
        if tax_bracket is None:
            return Response({"detail": "Tax bracket not found."}, status=404)
        serializer = TaxBracketSerializer(
            instance=tax_bracket,
            data=request.data,
            partial=True,
            context={"request": request},
        )
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=200)
        return Response(serializer.errors, status=400)

    def delete(self, request, pk):
        if not self._has_permission(request, "delete"):
            return Response({"detail": "You do not have permission."}, status=403)
        tax_bracket = self._queryset().filter(id=pk).first()
        if tax_bracket is None:
            return Response({"detail": "Tax bracket not found."}, status=404)
        tax_bracket.delete()
        return Response(status=200)


from datetime import datetime

from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from rest_framework import status
from rest_framework.authentication import SessionAuthentication

# DRF / Simple JWT imports
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication

from horilla.horilla_settings import HORILLA_DATE_FORMATS

# Your models / helpers
from payroll.models.models import Company, EmployeeWorkInformation, Payslip
from payroll.models.tax_models import PayrollSettings
from payroll.views.component_views import filter_payslip
from payroll.views.views import equalize_lists_length

try:
    import pdfkit

    HAVE_PDFKIT = True
except Exception:
    HAVE_PDFKIT = False


class PayslipPDFAPIView(APIView):
    """
    GET /api/payslip/<payslip_id>/?format=pdf
    Auth:
      - Accepts SimpleJWT Bearer token (Authorization: Bearer <token>)
      - Also accepts session auth (browser) when available
    """

    authentication_classes = (JWTAuthentication, SessionAuthentication)
    permission_classes = (IsAuthenticated,)

    def get(self, request, id, format=None):
        # get payslip or 404
        payslip = get_object_or_404(Payslip, id=id)

        # authorization: same logic as your view
        user = request.user
        if not (
            user.has_perm("payroll.view_payslip")
            or payslip.employee_id.employee_user_id == user
        ):
            return Response(
                {"detail": "You do not have permission to view this payslip."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # employee & company date format resolution
        employee = getattr(user, "employee_get", None) or payslip.employee_id
        info = EmployeeWorkInformation.objects.filter(employee_id=employee)
        if info.exists():
            # take the last one (mirrors your loop behavior)
            employee_company = info.last().company_id
            emp_company = Company.objects.filter(company=employee_company).first()
            date_format = (
                emp_company.date_format
                if emp_company and emp_company.date_format
                else "MMM. D, YYYY"
            )
        else:
            date_format = "MMM. D, YYYY"

        # compose data from payslip (same as original)
        data = (
            payslip.pay_head_data.copy()
            if isinstance(payslip.pay_head_data, dict)
            else {}
        )
        start_date_str = data.get("start_date")
        end_date_str = data.get("end_date")

        if not start_date_str or not end_date_str:
            return Response(
                {"detail": "Payslip missing start_date or end_date"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # parse dates
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()

        month_start_name = start_date.strftime("%B %d, %Y")
        month_end_name = end_date.strftime("%B %d, %Y")

        # formatted date for chosen company format (safe default if not found)
        formatted_start_date = start_date.strftime(
            HORILLA_DATE_FORMATS.get(date_format, "%b. %d, %Y")
        )
        formatted_end_date = end_date.strftime(
            HORILLA_DATE_FORMATS.get(date_format, "%b. %d, %Y")
        )

        # fill template context like original view
        data["month_start_name"] = month_start_name
        data["month_end_name"] = month_end_name
        data["formatted_start_date"] = formatted_start_date
        data["formatted_end_date"] = formatted_end_date
        data["employee"] = payslip.employee_id
        data["payslip"] = payslip
        data["json_data"] = data.copy()
        data["json_data"]["employee"] = payslip.employee_id.id
        data["json_data"]["payslip"] = payslip.id
        data["instance"] = payslip
        data["currency"] = (
            PayrollSettings.objects.first().currency_symbol
            if PayrollSettings.objects.exists()
            else "₹"
        )
        data["all_deductions"] = []
        for deduction_list in [
            data.get("basic_pay_deductions", []),
            data.get("gross_pay_deductions", []),
            data.get("pretax_deductions", []),
            data.get("post_tax_deductions", []),
            data.get("tax_deductions", []),
            data.get("net_deductions", []),
        ]:
            data["all_deductions"].extend(deduction_list)

        data["all_allowances"] = data.get("allowances", []).copy()
        # equalize lengths (your helper)
        equalize_lists_length(data.setdefault("allowances", []), data["all_deductions"])
        data["zipped_data"] = zip(data["allowances"], data["all_deductions"])
        data["host"] = request.get_host()
        data["protocol"] = "https" if request.is_secure() else "http"
        data["company"] = Company.objects.filter(hq=True).first()

        # render HTML string using template
        html = render_to_string(
            "payroll/payslip/payslip_pdf.html", context=data, request=request
        )

        # If client asked for PDF and pdfkit is available -> return PDF
        requested_format = request.GET.get("format", "").lower()
        if requested_format == "pdf":
            if not HAVE_PDFKIT:
                return Response(
                    {
                        "detail": "PDF generation not available on server. Install pdfkit/wkhtmltopdf."
                    },
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            try:
                # optional: configure pdfkit with path if needed
                pdf_options = {
                    "enable-local-file-access": None,  # if your template references local CSS
                }
                pdf_bytes = pdfkit.from_string(html, False, options=pdf_options)
                response = HttpResponse(pdf_bytes, content_type="application/pdf")
                response["Content-Disposition"] = f'inline; filename="payslip-{id}.pdf"'
                return response
            except Exception as e:
                return Response(
                    {"detail": f"PDF generation failed: {str(e)}"},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

        # default: return rendered HTML (use HttpResponse so browser sees it nicely)
        return HttpResponse(html)
