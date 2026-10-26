from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View
from django.views.generic import CreateView, DeleteView

from accounts.permissions import StaffRequiredMixin

from ..access import visible_deals
from ..forms import DocumentRequestForm, DocumentUploadForm
from ..models import Document, Note
from .shared import ParentDealMixin, VisibleDealChildMixin


def _can_access_document(user, doc: Document) -> bool:
    """Anyone who can see the document's deal — admins, the owning
    associate, or any applicant on the deal with a portal login."""
    return visible_deals(user).filter(pk=doc.deal_id).exists()


class DocumentCreateView(StaffRequiredMixin, ParentDealMixin, CreateView):
    """Staff: add a document request to a deal (file uploaded later)."""

    model = Document
    form_class = DocumentRequestForm

    def form_valid(self, form):
        form.instance.deal = self.parent_deal
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["parent_deal"] = self.parent_deal
        return ctx

    def get_success_url(self):
        return self.parent_deal.get_absolute_url()


class DocumentDeleteView(StaffRequiredMixin, VisibleDealChildMixin, DeleteView):
    """Staff: remove a document request (POST only — inline, no confirm page)."""

    model = Document

    def get_queryset(self):
        return super().get_queryset().select_related("deal")

    def get_success_url(self):
        return self.object.deal.get_absolute_url()


class DocumentUploadView(LoginRequiredMixin, View):
    """Attach a file to a document request. Used by both staff (deal page) and
    the customer (portal). Flips the document to 'provided'."""

    def post(self, request, pk):
        doc = get_object_or_404(Document.objects.select_related("deal__customer"), pk=pk)
        if not _can_access_document(request.user, doc):
            raise PermissionDenied

        form = DocumentUploadForm(request.POST, request.FILES)
        is_htmx = request.headers.get("HX-Request") == "true"
        if form.is_valid():
            files = form.cleaned_data["file"]
            doc.attach(files, by=request.user)
            if request.user.is_customer:
                # Audit trail for staff — mirrors the portal's inline edits.
                detail = f" ({len(files)} files)" if len(files) > 1 else ""
                Note.objects.create(
                    type=Note.Type.CUSTOMER_UPDATE,
                    author=request.user,
                    datetime=timezone.now(),
                    content=f"{request.user.full_name} uploaded “{doc.name}”{detail}.",
                    deal=doc.deal,
                )
            if is_htmx:
                # Portal inline upload: swap the document row in place. No
                # flash message — it would pop as a stale toast later.
                return render(request, "crm/_my_document_row.html", {"d": doc})
            messages.success(request, f"“{doc.name}” uploaded.")
        else:
            error = next(iter(form.errors.get("file", [])), "Please choose a file.")
            if is_htmx:
                return render(
                    request,
                    "crm/_my_document_row.html",
                    {"d": doc, "upload_error": error},
                )
            messages.error(request, f"Couldn't upload “{doc.name}”. {error}")

        if request.user.is_customer:
            return redirect("crm:my_deal_detail", pk=doc.deal_id)
        return redirect(doc.deal.get_absolute_url())


class DocumentDownloadView(LoginRequiredMixin, View):
    """Stream a document's file through Django so access is permission-checked
    (the storage bucket itself stays private)."""

    def get(self, request, pk):
        doc = get_object_or_404(Document.objects.select_related("deal__customer"), pk=pk)
        if not _can_access_document(request.user, doc):
            raise PermissionDenied
        if not doc.file:
            raise Http404("No file has been uploaded for this document yet.")
        filename = doc.file.name.rsplit("/", 1)[-1]
        # Zips (multi-file uploads) are saved, not rendered; PDFs open inline.
        is_zip = filename.lower().endswith(".zip")
        return FileResponse(doc.file.open("rb"), as_attachment=is_zip, filename=filename)
