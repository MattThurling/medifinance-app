from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.generic import CreateView, DeleteView, UpdateView

from accounts.permissions import StaffRequiredMixin

from ..access import scope_to_visible_deals, visible_deals
from ..forms import QuoteForm, TermsForm
from ..lender_status import emit_status_stage, notify_client_of_approval
from ..models import Deal, Quote, RateBand
from .shared import ParentDealMixin, VisibleDealChildMixin

# Quotes are always managed in the context of a parent Deal — no list/detail.
# On commercial deals (every type except Asset Finance) a quote is a "Terms"
# row — one lender's status and the terms they came back with — and the
# views emit the same stage events the Proposal views do.

class QuoteCreateView(StaffRequiredMixin, ParentDealMixin, CreateView):
    """Create a quote. Requires `?deal=<pk>` — the deal is set from URL, not
    the form. Asset Finance deals get the full rate-band form; every other
    type gets the Terms form."""

    model = Quote

    def get_form_class(self):
        return TermsForm if self.parent_deal.is_commercial_finance else QuoteForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["deal"] = self.parent_deal
        return kwargs

    def form_valid(self, form):
        # Terms can be recorded before a funded amount exists (the payment
        # just reads TBC); Asset Finance quotes are priced on it, so insist.
        if not self.parent_deal.is_commercial_finance and self.parent_deal.funded_amount is None:
            form.add_error(
                None,
                "This deal has no funded amount set, so we can't calculate a monthly payment. "
                "Edit the deal and add a supplier first.",
            )
            return self.form_invalid(form)
        form.instance.deal = self.parent_deal
        response = super().form_valid(form)
        if self.parent_deal.is_commercial_finance:
            emit_status_stage(
                self.parent_deal, self.object.status, self.object.lender, self.request.user,
            )
        return response

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["parent_deal"] = self.parent_deal
        if self.parent_deal.funded_amount is not None:
            ctx["funded_amount_display"] = f"£{self.parent_deal.funded_amount:,.2f}"
        return ctx

    def get_success_url(self):
        return self.parent_deal.get_absolute_url()


class QuoteUpdateView(StaffRequiredMixin, VisibleDealChildMixin, UpdateView):
    model = Quote

    def get_form_class(self):
        return TermsForm if self.object.deal.is_commercial_finance else QuoteForm

    def get_queryset(self):
        return super().get_queryset().select_related("deal")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["deal"] = self.object.deal
        return kwargs

    def form_valid(self, form):
        if not form.instance.deal.is_commercial_finance and form.instance.deal.funded_amount is None:
            form.add_error(
                None,
                "This deal has no funded amount set, so we can't recalculate the monthly payment. "
                "Edit the deal and add a supplier first.",
            )
            return self.form_invalid(form)
        old_status = form.initial.get("status")
        response = super().form_valid(form)
        deal = self.object.deal
        if deal.is_commercial_finance and old_status != self.object.status:
            emit_status_stage(deal, self.object.status, self.object.lender, self.request.user)
        return response

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["parent_deal"] = self.object.deal
        if self.object.deal.funded_amount is not None:
            ctx["funded_amount_display"] = f"£{self.object.deal.funded_amount:,.2f}"
        return ctx

    def get_success_url(self):
        return self.object.deal.get_absolute_url()


class QuoteDeleteView(StaffRequiredMixin, VisibleDealChildMixin, DeleteView):
    model = Quote

    def get_queryset(self):
        return super().get_queryset().select_related("deal")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["parent_deal"] = self.object.deal
        return ctx

    def get_success_url(self):
        return self.object.deal.get_absolute_url()


class QuoteSelectView(StaffRequiredMixin, View):
    """Staff equivalent of the portal quote-pick step — sets `Deal.selected_quote`
    from a radio button on the deal detail page."""

    def post(self, request, pk):
        quote = get_object_or_404(
            scope_to_visible_deals(Quote.objects.select_related("deal"), request.user), pk=pk
        )
        Deal.objects.filter(pk=quote.deal_id).update(selected_quote=quote)
        return redirect(quote.deal.get_absolute_url())


class QuoteNotifyClientView(StaffRequiredMixin, View):
    """Email the deal's customer the good news about approved Terms on a
    commercial deal — the Terms-row twin of ProposalNotifyClientView. Only
    the selected, Approved row, and only once (`Quote.notified_at`)."""

    def post(self, request, pk):
        quote = get_object_or_404(
            scope_to_visible_deals(
                Quote.objects.select_related(
                    "deal", "deal__customer", "deal__selected_quote", "lender",
                ),
                request.user,
            ),
            pk=pk,
        )
        deal = quote.deal
        if not deal.is_commercial_finance or quote.lender_id is None:
            messages.error(request, "Only terms on a commercial deal can be sent to the client.")
            return redirect(deal.get_absolute_url())
        return notify_client_of_approval(
            request,
            deal=deal,
            row=quote,
            lender=quote.lender,
            reference=quote.lender_reference,
            is_selected=deal.selected_quote_id == quote.pk,
            not_approved_msg="Only approved terms can be sent to the client.",
            not_selected_msg="Mark these terms as the selected terms before notifying the client.",
        )


class QuoteRateOptionsView(StaffRequiredMixin, View):
    """HTMX: <option>s for active rate bands matching ?term= and applicable to
    the quote's finance amount — the ?deal=`s funded amount minus the form's
    ?deposit= and ?balloon=. Drives the rate select on the quote form when the
    term, deposit or balloon changes."""

    @staticmethod
    def _amount_param(request, name) -> Decimal:
        raw = (request.GET.get(name) or "").replace(",", "").strip()
        try:
            return Decimal(raw) if raw else Decimal("0")
        except InvalidOperation:
            return Decimal("0")

    def get(self, request):
        term = request.GET.get("term", "")
        deal_pk = request.GET.get("deal", "")
        rates = []
        if term.isdigit():
            qs = (
                RateBand.objects.active()
                .select_related("organisation")
                .filter(term_months=int(term))
            )
            deal = (
                visible_deals(request.user).filter(pk=deal_pk).first()
                if deal_pk.isdigit() else None
            )
            amount = deal.funded_amount if deal else None
            if amount is not None:
                amount = amount - self._amount_param(request, "deposit") - self._amount_param(request, "balloon")
                qs = qs.filter(min_amount__lte=amount, max_amount__gte=amount)
            rates = qs.order_by("yield_percent")
        return render(request, "crm/_quote_rate_options.html", {"rates": rates})
