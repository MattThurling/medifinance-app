from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views import View
from django.views.generic import CreateView, UpdateView

from accounts.permissions import StaffRequiredMixin

from ..access import scope_to_visible_deals
from ..forms import ProposalForm
from ..models import Deal, Proposal, Stage
from .shared import ParentDealMixin, VisibleDealChildMixin

# Map a Proposal.Status -> the Stage.Name to emit when a proposal is created
# with, or its status flips to, that status. WITHDRAWN deliberately absent:
# withdrawing one proposal doesn't move the deal's stage — the proposal's own
# status records it.
PROPOSAL_STATUS_TO_STAGE = {
    Proposal.Status.SUBMITTED: Stage.Name.PROPOSAL_SUBMITTED,
    Proposal.Status.APPROVED: Stage.Name.PROPOSAL_APPROVED,
    Proposal.Status.DECLINED: Stage.Name.PROPOSAL_DECLINED,
}


# Proposals are managed in the context of a parent Deal (the broker shops the
# deal to many lenders), same shape as Quote — no separate list/detail page.

class ProposalSelectView(StaffRequiredMixin, View):
    """Set `Deal.selected_proposal` from a radio button on the deal detail page."""

    def post(self, request, pk):
        proposal = get_object_or_404(
            scope_to_visible_deals(Proposal.objects.select_related("deal"), request.user), pk=pk
        )
        Deal.objects.filter(pk=proposal.deal_id).update(selected_proposal=proposal)
        return redirect(proposal.deal.get_absolute_url())


class ProposalCreateView(StaffRequiredMixin, ParentDealMixin, CreateView):
    """Create a proposal. Requires `?deal=<pk>` — the deal is set from URL, not the form."""

    model = Proposal
    form_class = ProposalForm

    def form_valid(self, form):
        form.instance.deal = self.parent_deal
        response = super().form_valid(form)
        stage_name = PROPOSAL_STATUS_TO_STAGE.get(self.object.status)
        if stage_name:
            Stage.objects.create(
                deal=self.parent_deal,
                name=stage_name,
                organisation=self.object.lender,
                set_by=self.request.user,
            )
        return response

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["parent_deal"] = self.parent_deal
        return ctx

    def get_success_url(self):
        return self.parent_deal.get_absolute_url()


class ProposalUpdateView(StaffRequiredMixin, VisibleDealChildMixin, UpdateView):
    model = Proposal
    form_class = ProposalForm

    def get_queryset(self):
        return super().get_queryset().select_related("deal", "lender", "contact")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["parent_deal"] = self.object.deal
        return ctx

    def form_valid(self, form):
        old_status = form.initial.get("status")
        response = super().form_valid(form)
        new_status = self.object.status
        if old_status != new_status:
            stage_name = PROPOSAL_STATUS_TO_STAGE.get(new_status)
            if stage_name:
                Stage.objects.create(
                    deal=self.object.deal,
                    name=stage_name,
                    organisation=self.object.lender,
                    set_by=self.request.user,
                )
        return response

    def get_success_url(self):
        return self.object.deal.get_absolute_url()


class ProposalNotifyClientView(StaffRequiredMixin, View):
    """Email the deal's customer the good news about an approved proposal.

    Only allowed for the deal's selected proposal while it is Approved, and
    only once — `Proposal.notified_at` records (and blocks repeating) the send.
    """

    def post(self, request, pk):
        proposal = get_object_or_404(
            scope_to_visible_deals(
                Proposal.objects.select_related(
                    "deal", "deal__customer", "deal__selected_quote", "lender",
                ),
                request.user,
            ),
            pk=pk,
        )
        deal = proposal.deal

        if proposal.status != Proposal.Status.APPROVED:
            messages.error(request, "Only an approved proposal can be sent to the client.")
            return redirect(deal.get_absolute_url())
        if deal.selected_proposal_id != proposal.pk:
            messages.error(
                request,
                "Mark this proposal as the deal's selected proposal before notifying the client.",
            )
            return redirect(deal.get_absolute_url())
        if proposal.notified_at is not None:
            messages.error(
                request,
                f"The client was already notified on {proposal.notified_at:%d %b %Y}.",
            )
            return redirect(deal.get_absolute_url())
        if deal.customer is None or not deal.customer.email:
            messages.error(
                request,
                "Can't notify the client: this deal's customer has no email address on file.",
            )
            return redirect(deal.get_absolute_url())

        finance = deal.finance_amount
        quote = deal.selected_quote
        monthly = quote.monthly_payment if quote else None

        from accounts.emails import send_proposal_approved_client_email
        send_proposal_approved_client_email(
            to_email=deal.customer.email,
            contact_first_name=deal.customer.first_name,
            lender_org_name=proposal.lender.name,
            proposal_number=proposal.proposal_number,
            finance_amount_display=f"£{finance:,.2f}" if finance is not None else "",
            term_display=f"{quote.term} months" if quote else "",
            monthly_payment_display=f"£{monthly:,.2f}" if monthly is not None else "",
        )

        proposal.notified_at = timezone.now()
        proposal.save(update_fields=["notified_at", "updated_at"])
        Stage.objects.create(
            deal=deal,
            name=Stage.Name.CLIENT_NOTIFIED,
            organisation=proposal.lender,
            set_by=request.user,
        )
        messages.success(request, f"Approval email sent to {deal.customer.email}.")
        return redirect(deal.get_absolute_url())
