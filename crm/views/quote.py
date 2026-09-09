from decimal import Decimal, InvalidOperation

from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.generic import CreateView, DeleteView, UpdateView

from accounts.permissions import StaffRequiredMixin

from ..forms import CommercialQuoteForm, QuoteForm
from ..models import Deal, Quote, RateBand

# Quotes are always managed in the context of a parent Deal — no list/detail.

class QuoteCreateView(StaffRequiredMixin, CreateView):
    """Create a quote. Requires `?deal=<pk>` — the deal is set from URL, not
    the form. Asset Finance deals get the full rate-band form; every other
    type gets the slim lender/term/commitment form."""

    model = Quote

    def get_form_class(self):
        return CommercialQuoteForm if self.parent_deal.is_commercial_finance else QuoteForm

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        deal_pk = request.GET.get("deal")
        if not deal_pk:
            raise Http404("?deal query parameter required")
        try:
            self.parent_deal = Deal.objects.get(pk=deal_pk)
        except (Deal.DoesNotExist, ValueError, TypeError) as exc:
            raise Http404("Deal not found") from exc

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["deal"] = self.parent_deal
        return kwargs

    def form_valid(self, form):
        # Commercial quotes carry no pricing maths, so no funded amount is needed.
        if not self.parent_deal.is_commercial_finance and self.parent_deal.funded_amount is None:
            form.add_error(
                None,
                "This deal has no funded amount set, so we can't calculate a monthly payment. "
                "Edit the deal and add a supplier first.",
            )
            return self.form_invalid(form)
        form.instance.deal = self.parent_deal
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["parent_deal"] = self.parent_deal
        if self.parent_deal.funded_amount is not None:
            ctx["funded_amount_display"] = f"£{self.parent_deal.funded_amount:,.2f}"
        return ctx

    def get_success_url(self):
        return self.parent_deal.get_absolute_url()


class QuoteUpdateView(StaffRequiredMixin, UpdateView):
    model = Quote

    def get_form_class(self):
        return CommercialQuoteForm if self.object.deal.is_commercial_finance else QuoteForm

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
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["parent_deal"] = self.object.deal
        if self.object.deal.funded_amount is not None:
            ctx["funded_amount_display"] = f"£{self.object.deal.funded_amount:,.2f}"
        return ctx

    def get_success_url(self):
        return self.object.deal.get_absolute_url()


class QuoteDeleteView(StaffRequiredMixin, DeleteView):
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
        quote = get_object_or_404(Quote.objects.select_related("deal"), pk=pk)
        Deal.objects.filter(pk=quote.deal_id).update(selected_quote=quote)
        return redirect(quote.deal.get_absolute_url())


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
            deal = Deal.objects.filter(pk=deal_pk).first() if deal_pk.isdigit() else None
            amount = deal.funded_amount if deal else None
            if amount is not None:
                amount = amount - self._amount_param(request, "deposit") - self._amount_param(request, "balloon")
                qs = qs.filter(min_amount__lte=amount, max_amount__gte=amount)
            rates = qs.order_by("yield_percent")
        return render(request, "crm/_quote_rate_options.html", {"rates": rates})
