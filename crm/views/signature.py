import json
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.core.files.base import ContentFile
from django.db import transaction
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.utils.decorators import method_decorator
from django.utils.text import slugify
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from accounts.permissions import StaffRequiredMixin

from .. import docuseal
from ..access import scope_to_visible_deals, visible_deals
from ..forms import SignatureRequestForm
from ..models import Contact, SignatureRequest
from .shared import ParentDealMixin


logger = logging.getLogger(__name__)


def _can_access_signature(user, sr: SignatureRequest) -> bool:
    """Anyone who can see the request's deal — admins, the owning associate,
    or the deal's customer."""
    return visible_deals(user).filter(pk=sr.deal_id).exists()


@method_decorator(csrf_exempt, name="dispatch")
class DocuSealWebhookView(View):
    """Receives DocuSeal's form.* events and advances the matching
    SignatureRequest. Auth is a shared secret header configured on the
    DocuSeal console (no session, so CSRF is exempted).

    Contract with DocuSeal's retry loop: 200 acknowledges (including events
    we don't care about or can't match — retrying those can never help),
    4xx means the request itself is bad, 502 asks for a retry (we couldn't
    reach DocuSeal back to fetch the signed files)."""

    def post(self, request):
        if not settings.DOCUSEAL_WEBHOOK_SECRET:
            return JsonResponse({"error": "unconfigured"}, status=503)
        if not constant_time_compare(
            request.headers.get("X-Docuseal-Secret", ""),
            settings.DOCUSEAL_WEBHOOK_SECRET,
        ):
            return JsonResponse({"error": "forbidden"}, status=401)

        try:
            payload = json.loads(request.body)
            event = payload["event_type"]
            data = payload.get("data") or {}
        except (ValueError, TypeError, KeyError):
            return JsonResponse({"error": "bad_payload"}, status=400)

        # form.* events carry the submitter (with its parent submission_id);
        # submission.* events carry the submission itself.
        submission_id = data.get("submission_id") if event.startswith("form.") else data.get("id")
        if not submission_id:
            return JsonResponse({"ok": True})

        try:
            with transaction.atomic():
                sr = (
                    SignatureRequest.objects
                    .select_for_update()
                    .filter(submission_id=submission_id)
                    .first()
                )
                if sr is None:
                    logger.info("DocuSeal webhook %s for unknown submission %s", event, submission_id)
                elif event in ("form.viewed", "form.started"):
                    if sr.status == SignatureRequest.Status.SENT:
                        sr.status = SignatureRequest.Status.OPENED
                        sr.opened_at = timezone.now()
                        sr.save(update_fields=["status", "opened_at", "updated_at"])
                elif event == "form.declined":
                    if sr.status not in (SignatureRequest.Status.COMPLETED, SignatureRequest.Status.VOIDED):
                        sr.status = SignatureRequest.Status.DECLINED
                        sr.declined_at = timezone.now()
                        sr.decline_reason = data.get("decline_reason") or ""
                        sr.save(update_fields=["status", "declined_at", "decline_reason", "updated_at"])
                elif event in ("form.completed", "submission.completed"):
                    self._handle_completed(sr, data)
        except docuseal.DocuSealError as exc:
            logger.error("DocuSeal webhook for submission %s: %s", submission_id, exc)
            return JsonResponse({"error": "docuseal_unreachable"}, status=502)

        return JsonResponse({"ok": True})

    @staticmethod
    def _handle_completed(sr: SignatureRequest, data: dict) -> None:
        # Duplicate delivery (or form.completed followed by submission.completed)
        # must be a no-op once the signed file is in.
        if sr.status == SignatureRequest.Status.COMPLETED and sr.signed_file:
            return

        # Verify-then-fetch: treat our own API call as the source of truth
        # rather than the webhook body someone POSTed at us.
        submission = docuseal.get_submission(sr.submission_id)
        if submission.get("status") != "completed" and not submission.get("completed_at"):
            logger.warning(
                "DocuSeal sent a completed event for submission %s but the API says %r",
                sr.submission_id, submission.get("status"),
            )
            return

        files = docuseal.get_submission_documents(sr.submission_id)
        if files:
            pdf = docuseal.download_file(files[0]["url"])
            base = slugify(sr.template_name) or "agreement"
            sr.signed_file.save(f"{base}-signed.pdf", ContentFile(pdf), save=False)

        audit_url = submission.get("audit_log_url") or (data.get("submission") or {}).get("audit_log_url")
        if audit_url and not sr.audit_log_file:
            sr.audit_log_file.save("audit-log.pdf", ContentFile(docuseal.download_file(audit_url)), save=False)

        submitter = (submission.get("submitters") or [{}])[0]
        sr.status = SignatureRequest.Status.COMPLETED
        sr.completed_at = timezone.now()
        sr.signer_ip = submitter.get("ip") or data.get("ip") or None
        sr.signer_user_agent = (submitter.get("ua") or data.get("ua") or "")[:512]
        sr.save()


class SignatureRequestCreateView(StaffRequiredMixin, ParentDealMixin, View):
    """Staff: send a deal off for e-signature. The deal comes from the URL
    pk; `ParentDealMixin` 404s if this user can't see it."""

    template_name = "crm/signature_request_form.html"
    parent_deal_from_url = True
    parent_deal_select_related = ("customer",)

    @property
    def deal(self):
        return self.parent_deal

    def _unconfigured(self):
        """Redirect back to the deal when DocuSeal isn't set up, else None."""
        if docuseal.is_configured():
            return None
        messages.error(self.request, "DocuSeal isn't configured on this environment yet.")
        return redirect(self.deal.get_absolute_url())

    def _signers(self):
        pks = [c.pk for c in (self.deal.customer, *self.deal.co_applicants.all()) if c]
        return Contact.objects.filter(pk__in=pks)

    def _form(self, data=None):
        try:
            templates = docuseal.list_templates()
        except docuseal.DocuSealError as exc:
            messages.error(self.request, f"Couldn't reach DocuSeal: {exc}")
            return None
        if not templates:
            messages.error(self.request, "No templates exist on DocuSeal yet — build one there first.")
            return None
        customer = self.deal.customer
        initial = {
            "signer": customer,
            "signer_email": customer.email if customer else "",
            "signer_name": customer.full_name if customer else "",
        }
        return SignatureRequestForm(data, initial=initial, templates=templates, signers=self._signers())

    def get(self, request, **kwargs):
        if (resp := self._unconfigured()) is not None:
            return resp
        form = self._form()
        if form is None:
            return redirect(self.deal.get_absolute_url())
        return render(request, self.template_name, {"form": form, "parent_deal": self.deal})

    def post(self, request, **kwargs):
        if (resp := self._unconfigured()) is not None:
            return resp
        form = self._form(request.POST)
        if form is None:
            return redirect(self.deal.get_absolute_url())
        if not form.is_valid():
            return render(request, self.template_name, {"form": form, "parent_deal": self.deal})

        template_id = int(form.cleaned_data["template"])
        template_name = dict(form.fields["template"].choices).get(template_id, "")
        try:
            result = docuseal.create_submission(
                template_id=template_id,
                signer_email=form.cleaned_data["signer_email"],
                signer_name=form.cleaned_data["signer_name"],
                values=docuseal.build_prefill_values(self.deal),
                message=form.cleaned_data["message"] or None,
            )
        except docuseal.DocuSealError as exc:
            messages.error(request, f"DocuSeal rejected the request: {exc}")
            return render(request, self.template_name, {"form": form, "parent_deal": self.deal})

        SignatureRequest.objects.create(
            deal=self.deal,
            template_id=template_id,
            template_name=template_name,
            submission_id=result["submission_id"],
            submitter_id=result["submitter_id"],
            signing_slug=result.get("slug") or "",
            signer=form.cleaned_data["signer"],
            signer_email=form.cleaned_data["signer_email"],
            signer_name=form.cleaned_data["signer_name"],
            created_by=request.user,
        )
        messages.success(
            request,
            f"“{template_name or 'Signature request'}” sent to {form.cleaned_data['signer_email']} for signature.",
        )
        return redirect(self.deal.get_absolute_url())


class SignatureRequestVoidView(StaffRequiredMixin, View):
    """Staff: cancel an in-flight signature request (POST only — inline)."""

    def post(self, request, pk):
        sr = get_object_or_404(
            scope_to_visible_deals(SignatureRequest.objects.select_related("deal"), request.user),
            pk=pk,
        )
        if not sr.is_active:
            messages.error(request, "Only a pending signature request can be voided.")
            return redirect(sr.deal.get_absolute_url())
        try:
            docuseal.archive_submission(sr.submission_id)
        except docuseal.DocuSealError as exc:
            messages.error(request, f"Couldn't void on DocuSeal: {exc}")
            return redirect(sr.deal.get_absolute_url())
        sr.status = SignatureRequest.Status.VOIDED
        sr.save(update_fields=["status", "updated_at"])
        messages.success(request, f"Signature request “{sr.template_name or sr.submission_id}” voided.")
        return redirect(sr.deal.get_absolute_url())


class SignatureRequestResendView(StaffRequiredMixin, View):
    """Staff: re-send a pending or declined request — voids the old DocuSeal
    submission and creates a fresh one to the same signer (POST only)."""

    def post(self, request, pk):
        old = get_object_or_404(
            scope_to_visible_deals(SignatureRequest.objects.select_related("deal"), request.user),
            pk=pk,
        )
        deal = old.deal
        if old.status == SignatureRequest.Status.COMPLETED:
            messages.error(request, "That request has already been completed.")
            return redirect(deal.get_absolute_url())
        try:
            if old.is_active:
                docuseal.archive_submission(old.submission_id)
            result = docuseal.create_submission(
                template_id=old.template_id,
                signer_email=old.signer_email,
                signer_name=old.signer_name,
                values=docuseal.build_prefill_values(deal),
            )
        except docuseal.DocuSealError as exc:
            messages.error(request, f"Couldn't re-send via DocuSeal: {exc}")
            return redirect(deal.get_absolute_url())
        if old.is_active:
            old.status = SignatureRequest.Status.VOIDED
            old.save(update_fields=["status", "updated_at"])
        SignatureRequest.objects.create(
            deal=deal,
            template_id=old.template_id,
            template_name=old.template_name,
            submission_id=result["submission_id"],
            submitter_id=result["submitter_id"],
            signing_slug=result.get("slug") or "",
            signer=old.signer,
            signer_email=old.signer_email,
            signer_name=old.signer_name,
            created_by=request.user,
        )
        messages.success(
            request,
            f"“{old.template_name or 'Signature request'}” re-sent to {old.signer_email} for signature.",
        )
        return redirect(deal.get_absolute_url())


class SignatureSignedFileDownloadView(LoginRequiredMixin, View):
    """Stream the signed PDF for a completed signature request. Staff see any;
    a customer only their own deal's (linked from the customer portal)."""

    def get(self, request, pk):
        sr = get_object_or_404(SignatureRequest.objects.select_related("deal__customer"), pk=pk)
        if not _can_access_signature(request.user, sr):
            raise PermissionDenied
        if not sr.signed_file:
            raise Http404("No signed document has been stored for this signature request.")
        filename = sr.signed_file.name.rsplit("/", 1)[-1]
        return FileResponse(sr.signed_file.open("rb"), as_attachment=False, filename=filename)


class SignatureAuditDownloadView(LoginRequiredMixin, View):
    """Stream the DocuSeal audit-log PDF for a completed signature request."""

    def get(self, request, pk):
        sr = get_object_or_404(SignatureRequest.objects.select_related("deal__customer"), pk=pk)
        if not _can_access_signature(request.user, sr):
            raise PermissionDenied
        if not sr.audit_log_file:
            raise Http404("No audit log has been stored for this signature request.")
        return FileResponse(sr.audit_log_file.open("rb"), as_attachment=False, filename="audit-log.pdf")
