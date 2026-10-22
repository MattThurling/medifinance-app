from django.shortcuts import get_object_or_404, redirect
from django.views import View
from django.views.generic import CreateView, UpdateView

from accounts.permissions import StaffRequiredMixin

from ..access import scope_to_visible_deals
from ..forms import ProposalForm
from ..lender_status import (
    LENDER_STATUS_TO_STAGE,
    emit_status_stage,
    notify_client_of_approval,
)
from ..models import Deal, Proposal
from .shared import ParentDealMixin, VisibleDealChildMixin

# Status -> stage mapping now lives in crm.lender_status (shared with the
# Terms rows on commercial deals); kept under its old name for importers.
PROPOSAL_STATUS_TO_STAGE = LENDER_STATUS_TO_STAGE


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
        emit_status_stage(
            self.parent_deal, self.object.status, self.object.lender, self.request.user,
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
        if old_status != self.object.status:
            emit_status_stage(
                self.object.deal, self.object.status, self.object.lender, self.request.user,
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
        return notify_client_of_approval(
            request,
            deal=deal,
            row=proposal,
            lender=proposal.lender,
            reference=proposal.proposal_number,
            is_selected=deal.selected_proposal_id == proposal.pk,
            not_approved_msg="Only an approved proposal can be sent to the client.",
            not_selected_msg=(
                "Mark this proposal as the deal's selected proposal before notifying the client."
            ),
        )
