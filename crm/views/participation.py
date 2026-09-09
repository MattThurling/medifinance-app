import logging

from django.contrib import messages
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View
from django.views.generic import CreateView, DeleteView, UpdateView

from accounts.permissions import StaffRequiredMixin

from ..forms import ParticipationForm, SupplierInvoiceForm
from ..models import Deal, Participation, ParticipationInvoiceLink, Stage


logger = logging.getLogger(__name__)

# Participations represent suppliers contributing to a deal's funded amount.
# Managed in the context of a parent Deal, same pattern as Quote / Proposal.

class ParticipationCreateView(StaffRequiredMixin, CreateView):
    """Create a participation. Requires `?deal=<pk>` — the deal is set from URL, not the form."""

    model = Participation
    form_class = ParticipationForm

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        deal_pk = request.GET.get("deal")
        if not deal_pk:
            raise Http404("?deal query parameter required")
        try:
            self.parent_deal = Deal.objects.get(pk=deal_pk)
        except (Deal.DoesNotExist, ValueError, TypeError) as exc:
            raise Http404("Deal not found") from exc

    def form_valid(self, form):
        form.instance.deal = self.parent_deal
        return super().form_valid(form)

    def get_form_kwargs(self):
        return super().get_form_kwargs() | {"deal": self.parent_deal}

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["parent_deal"] = self.parent_deal
        return ctx

    def get_success_url(self):
        return self.parent_deal.get_absolute_url()


class ParticipationUpdateView(StaffRequiredMixin, UpdateView):
    model = Participation
    form_class = ParticipationForm

    def get_queryset(self):
        return super().get_queryset().select_related("deal", "organisation", "invoice_contact")

    def get_form_kwargs(self):
        return super().get_form_kwargs() | {"deal": self.object.deal}

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["parent_deal"] = self.object.deal
        return ctx

    def get_success_url(self):
        return self.object.deal.get_absolute_url()


class ParticipationDeleteView(StaffRequiredMixin, DeleteView):
    """Staff: remove a participation (POST only — inline, no confirm page)."""

    model = Participation

    def get_queryset(self):
        return super().get_queryset().select_related("deal")

    def get_success_url(self):
        return self.object.deal.get_absolute_url()


class ParticipationInvoiceDownloadView(StaffRequiredMixin, View):
    """Stream a supplier's invoice PDF inline. Goes through Django so the
    private GCS bucket stays private — same pattern as DocumentDownloadView."""

    def get(self, request, pk):
        p = get_object_or_404(Participation.objects.select_related("deal"), pk=pk)
        if not p.invoice:
            raise Http404("No invoice file uploaded yet.")
        filename = p.invoice.name.rsplit("/", 1)[-1]
        return FileResponse(p.invoice.open("rb"), as_attachment=False, filename=filename)


class RequestParticipationInvoiceView(StaffRequiredMixin, View):
    """Staff: mint a single-use upload link for a supplier and email it to their
    invoice contact. Requires a selected Proposal on the deal so the email body
    can name the accepted lender."""

    def post(self, request, pk):
        participation = get_object_or_404(
            Participation.objects.select_related(
                "deal", "deal__organisation", "deal__customer",
                "deal__selected_proposal", "deal__selected_proposal__lender",
                "organisation", "invoice_contact",
            ),
            pk=pk,
        )

        contact = participation.invoice_contact
        if contact is None or not contact.email:
            messages.error(
                request,
                "Set an invoice contact (with an email address) on this supplier first.",
            )
            return redirect(participation.deal.get_absolute_url())

        selected = participation.deal.selected_proposal
        if selected is None:
            messages.error(
                request,
                "Select a Proposal on this deal before sending an invoice request — "
                "the email needs to name the accepted lender.",
            )
            return redirect(participation.deal.get_absolute_url())

        client_org = participation.deal.organisation
        if client_org is None:
            messages.error(
                request,
                "Set the deal's Organisation before sending an invoice request.",
            )
            return redirect(participation.deal.get_absolute_url())

        link = ParticipationInvoiceLink.issue(
            participation=participation, created_by=request.user
        )
        full_url = request.build_absolute_uri(
            reverse("crm:participation_submit_invoice", args=[link.token])
        )

        from accounts.emails import send_supplier_invoice_request_email
        send_supplier_invoice_request_email(
            to_email=contact.email,
            link_url=full_url,
            contact_first_name=(contact.first_name or "").strip(),
            lead_contact_name=str(participation.deal.customer),
            client_org_name=client_org.formal_name,
            client_org_address=client_org.display_address,
            lender_org_name=selected.lender.name,
            lender_org_address=selected.lender.display_address,
            participation_amount_display=f"£{participation.amount:,.2f}",
            participation_description=(participation.description or "").strip(),
            expires_at=link.expires_at,
        )

        Stage.objects.create(
            deal=participation.deal,
            name=Stage.Name.SUPPLIER_INVOICE_REQUESTED,
            organisation=participation.organisation,
            set_by=request.user,
        )

        messages.success(
            request,
            f"Invoice request emailed to {contact.email}. "
            f"Link expires {link.expires_at:%d %b %Y, %H:%M}.",
        )
        return redirect(participation.deal.get_absolute_url())


class SubmitParticipationInvoiceView(View):
    """Public — the supplier follows the email link, lands here, and uploads
    their invoice. The token is the auth; no login required."""

    template_name = "crm/supplier_invoice_submit.html"
    invalid_template_name = "crm/supplier_invoice_invalid.html"
    complete_template_name = "crm/supplier_invoice_complete.html"

    def _get_link(self, token):
        return ParticipationInvoiceLink.objects.select_related(
            "participation", "participation__deal", "participation__deal__organisation"
        ).filter(token=token).first()

    def _client_ip(self, request):
        # Cloud Run terminates TLS; X-Forwarded-For has the original client.
        xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
        return xff.split(",")[0].strip() if xff else request.META.get("REMOTE_ADDR")

    def get(self, request, token):
        link = self._get_link(token)
        if link is None:
            return render(request, self.invalid_template_name,
                          {"reason": "We can't find that invoice link — it may have been mistyped."},
                          status=404)
        if link.is_consumed:
            return render(request, self.invalid_template_name,
                          {"reason": "This link has already been used."}, status=410)
        if link.is_expired:
            return render(request, self.invalid_template_name,
                          {"reason": "This link has expired."}, status=410)

        form = SupplierInvoiceForm(instance=link.participation)
        return render(request, self.template_name,
                      {"form": form, "link": link, "participation": link.participation})

    def post(self, request, token):
        link = self._get_link(token)
        if link is None or not link.is_valid:
            reason = ("This link has already been used." if link and link.is_consumed
                      else "This link has expired." if link and link.is_expired
                      else "We can't find that invoice link.")
            return render(request, self.invalid_template_name, {"reason": reason}, status=410)

        form = SupplierInvoiceForm(request.POST, request.FILES, instance=link.participation)
        if form.is_valid():
            form.save()
            link.consume(ip=self._client_ip(request))
            participation = link.participation
            deal = participation.deal
            Stage.objects.create(
                deal=deal,
                name=Stage.Name.SUPPLIER_INVOICE_RECEIVED,
                organisation=participation.organisation,
                # No set_by — the supplier isn't a logged-in user.
            )
            try:
                from accounts.emails import send_supplier_invoice_submitted_email
                send_supplier_invoice_submitted_email(
                    deal_name=deal.name,
                    deal_url=request.build_absolute_uri(deal.get_absolute_url()),
                    supplier_name=participation.organisation.name if participation.organisation else "",
                    client_org_name=deal.organisation.name if deal.organisation else "",
                    amount_display=f"£{participation.amount:,.2f}",
                    invoice_number=participation.invoice_number or "",
                )
            except Exception:
                logger.exception("Supplier-invoice notification email failed for participation %s", participation.pk)
            return render(request, self.complete_template_name,
                          {"participation": participation})
        return render(request, self.template_name,
                      {"form": form, "link": link, "participation": link.participation})
