from django.contrib import messages
from django.db import transaction
from django.shortcuts import redirect, render
from django.views import View
from django.views.generic import TemplateView

from accounts.permissions import StaffRequiredMixin

from ..forms import RateBandForm, RateLookupForm, RateUploadForm
from ..models import Organisation, RateBand


class RatesView(StaffRequiredMixin, TemplateView):
    """Staff rate lookup: enter a term + amount, see every active band that
    applies, ordered by rate-per-thousand descending."""

    template_name = "crm/rates.html"

    def get_context_data(self, **kwargs):
        from decimal import Decimal as D

        ctx = super().get_context_data(**kwargs)
        form = RateLookupForm(self.request.GET or None)
        ctx["form"] = form

        if self.request.GET and form.is_valid():
            term = form.cleaned_data["term_months"]
            amount = form.cleaned_data["amount"]
            bands = (
                RateBand.objects.active()
                .select_related("organisation")
                .filter(term_months=term, min_amount__lte=amount, max_amount__gte=amount)
            )
            # rate_per_thousand is a computed property, so sort in Python (ascending).
            results = sorted(bands, key=lambda b: b.rate_per_thousand)
            for b in results:
                b.monthly = (b.rate_per_thousand * amount / D("1000")).quantize(D("0.01"))
            ctx["results"] = results
            ctx["searched"] = True
            ctx["term"] = term
            ctx["amount"] = amount
        return ctx


class RateUploadView(StaffRequiredMixin, View):
    """Upload a lender's rate sheet CSV and upsert RateBand rows.

    CSV columns: minimum, maximum, then one column per term (12, 24, …). Each
    non-blank term cell becomes a band. Idempotent — keyed on
    (org, term, min, max), so re-uploading refreshes yields, same as the
    load_bnp_rates command.
    """

    template_name = "crm/rate_upload.html"

    def get(self, request):
        return render(request, self.template_name, {"form": RateUploadForm()})

    def post(self, request):
        form = RateUploadForm(request.POST, request.FILES)
        if form.is_valid():
            org = form.cleaned_data["organisation"]
            bands = form.cleaned_data["bands"]
            counts = {"new": 0, "changed": 0, "unchanged": 0}
            with transaction.atomic():
                for lo, hi, term, y in bands:
                    counts[RateBand.record(
                        organisation=org,
                        term_months=term,
                        min_amount=lo,
                        max_amount=hi,
                        yield_percent=y,
                    )] += 1
            messages.success(
                request,
                f"{org.name}: {counts['new']} new, {counts['changed']} changed, "
                f"{counts['unchanged']} unchanged.",
            )
            return redirect("crm:rates")

        # Re-render with the chosen lender preserved in the combobox.
        pk = request.POST.get("organisation")
        org = Organisation.objects.filter(pk=pk).first() if pk else None
        return render(
            request,
            self.template_name,
            {"form": form, "selected_org_id": pk or "", "selected_org_label": org or ""},
        )


class RateBandAddView(StaffRequiredMixin, View):
    """Add a single rate band. Routes through RateBand.record() so manual adds
    get the same history-preserving behaviour as the CSV upload (an existing
    active band for the same key is superseded rather than overwritten)."""

    template_name = "crm/rate_band_form.html"

    def get(self, request):
        return render(request, self.template_name, {"form": RateBandForm()})

    def post(self, request):
        form = RateBandForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            status = RateBand.record(
                organisation=cd["organisation"],
                term_months=cd["term_months"],
                min_amount=cd["min_amount"],
                max_amount=cd["max_amount"],
                yield_percent=cd["yield_percent"],
            )
            messages.success(
                request,
                f"Rate {status} for {cd['organisation'].name}: {cd['term_months']}m, "
                f"£{cd['min_amount']:,}–£{cd['max_amount']:,} @ {cd['yield_percent']}%.",
            )
            return redirect("crm:rates")

        pk = request.POST.get("organisation")
        org = Organisation.objects.filter(pk=pk).first() if pk else None
        return render(
            request,
            self.template_name,
            {"form": form, "selected_org_id": pk or "", "selected_org_label": org or ""},
        )
