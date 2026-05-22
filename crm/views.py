from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import ProtectedError, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.views import View
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from accounts.models import MagicLink, Role
from accounts.permissions import StaffRequiredMixin

from .forms import (
    CoApplicantFormSet,
    CompanyInfoForm,
    ContactForm,
    CustomerInfoForm,
    DealForm,
    OrganisationForm,
    QuoteForm,
    QuoteSelectionForm,
    StageForm,
)
from .models import Contact, Deal, Organisation, Quote, Stage


class SearchableListView(ListView):
    """ListView that supports `?q=` filtering via `search_fields`."""

    paginate_by = 25
    search_fields: list[str] = []

    def get_queryset(self):
        qs = super().get_queryset()
        q = self.request.GET.get("q", "").strip()
        if q and self.search_fields:
            cond = Q()
            for field in self.search_fields:
                cond |= Q(**{f"{field}__icontains": q})
            qs = qs.filter(cond)
        return qs.distinct()

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["q"] = self.request.GET.get("q", "")
        return ctx


class ProtectedDeleteMixin:
    """Convert ProtectedError into a friendly redirect with a flash message."""

    protected_redirect_url: str = ""
    protected_message: str = "Can't delete this — other records still reference it."

    def post(self, request, *args, **kwargs):
        try:
            return super().post(request, *args, **kwargs)
        except ProtectedError:
            messages.error(request, self.protected_message)
            return redirect(self.protected_redirect_url or self.get_object().get_absolute_url())


# --- Organisation -----------------------------------------------------------

class OrganisationListView(StaffRequiredMixin, SearchableListView):
    model = Organisation
    search_fields = ["name", "hubspot_id"]


class OrganisationDetailView(StaffRequiredMixin, DetailView):
    model = Organisation

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["contacts"] = self.object.contacts.all()
        ctx["deals"] = Deal.objects.filter(customer__organisation=self.object).select_related("owner", "customer")
        return ctx


class OrganisationCreateView(StaffRequiredMixin, CreateView):
    model = Organisation
    form_class = OrganisationForm

    def get_success_url(self):
        return self.object.get_absolute_url()


class OrganisationUpdateView(StaffRequiredMixin, UpdateView):
    model = Organisation
    form_class = OrganisationForm

    def get_success_url(self):
        return self.object.get_absolute_url()


class OrganisationDeleteView(StaffRequiredMixin, ProtectedDeleteMixin, DeleteView):
    model = Organisation
    success_url = reverse_lazy("crm:organisation_list")
    protected_message = "Can't delete this organisation — it still has contacts linked to it."

    @property
    def protected_redirect_url(self):
        return self.object.get_absolute_url()


# --- Contact ----------------------------------------------------------------

class ContactListView(StaffRequiredMixin, SearchableListView):
    model = Contact
    search_fields = ["first_name", "last_name", "email", "hubspot_id", "organisation__name"]

    def get_queryset(self):
        return super().get_queryset().select_related("organisation")


class ContactDetailView(StaffRequiredMixin, DetailView):
    model = Contact

    def get_queryset(self):
        return super().get_queryset().select_related("organisation", "user")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["deals"] = self.object.deals.select_related("owner").all()
        return ctx


class ContactCreateView(StaffRequiredMixin, CreateView):
    """Create a contact. Accepts `?organisation=<pk>` to prefill + lock the org FK
    (when launched from an organisation's detail page)."""

    model = Contact
    form_class = ContactForm

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.parent_organisation = None
        org_pk = request.GET.get("organisation")
        if org_pk:
            try:
                self.parent_organisation = Organisation.objects.get(pk=org_pk)
            except (Organisation.DoesNotExist, ValueError, TypeError):
                pass

    def get_initial(self):
        initial = super().get_initial()
        if self.parent_organisation:
            initial["organisation"] = self.parent_organisation
        return initial

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["parent_organisation"] = self.parent_organisation
        return ctx

    def get_success_url(self):
        if self.parent_organisation:
            return self.parent_organisation.get_absolute_url()
        return self.object.get_absolute_url()


class ContactUpdateView(StaffRequiredMixin, UpdateView):
    model = Contact
    form_class = ContactForm

    def get_success_url(self):
        return self.object.get_absolute_url()


class ContactDeleteView(StaffRequiredMixin, ProtectedDeleteMixin, DeleteView):
    model = Contact
    success_url = reverse_lazy("crm:contact_list")
    protected_message = "Can't delete this contact — they're the customer on one or more deals."


# --- Deal -------------------------------------------------------------------

class DealListView(StaffRequiredMixin, SearchableListView):
    model = Deal
    search_fields = [
        "name",
        "hubspot_id",
        "customer__first_name",
        "customer__last_name",
        "customer__email",
        "customer__organisation__name",
    ]

    def get_queryset(self):
        # Annotate each deal with its current stage code so the list page
        # doesn't have to fire one query per row.
        from django.db.models import OuterRef, Subquery
        latest_stage = (
            Stage.objects.filter(deal=OuterRef("pk")).order_by("-occurred_at", "-pk").values("name")[:1]
        )
        return (
            super()
            .get_queryset()
            .select_related("owner", "customer", "customer__organisation")
            .annotate(current_stage_name=Subquery(latest_stage))
        )


class DealDetailView(StaffRequiredMixin, DetailView):
    model = Deal

    def get_queryset(self):
        return super().get_queryset().select_related(
            "owner",
            "customer",
            "customer__organisation",
            "introducer",
            "introducer__organisation",
            "equipment_supplier",
        ).prefetch_related("quotes", "stage_events", "co_applicants")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        deal = self.object

        # Stage history oldest-first for the vertical steps timeline
        ctx["stages_chrono"] = deal.stage_events.order_by("occurred_at", "pk")

        # Applicants = the lead (customer) plus any co-applicants
        ctx["applicants"] = [deal.customer, *deal.co_applicants.all()]

        # Split the amount so the pence can render smaller (£25,000.00)
        if deal.funded_amount is not None:
            pounds = int(deal.funded_amount)
            pence = int((deal.funded_amount - pounds) * 100)
            ctx["amount_pounds"] = f"{pounds:,}"
            ctx["amount_pence"] = f"{pence:02d}"
        return ctx


class _DealFormMixin:
    form_class = DealForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["current_user"] = self.request.user
        return kwargs

    def get_success_url(self):
        return self.object.get_absolute_url()


class DealCreateView(StaffRequiredMixin, _DealFormMixin, CreateView):
    """Create a deal. Accepts `?customer=<pk>` to prefill + lock the customer FK
    (when launched from a contact's detail page)."""

    model = Deal

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.parent_contact = None
        contact_pk = request.GET.get("customer")
        if contact_pk:
            try:
                self.parent_contact = Contact.objects.get(pk=contact_pk)
            except (Contact.DoesNotExist, ValueError, TypeError):
                pass

    def get_initial(self):
        initial = super().get_initial()
        if self.parent_contact:
            initial["customer"] = self.parent_contact
        return initial

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["parent_contact"] = self.parent_contact
        return ctx

    def get_success_url(self):
        if self.parent_contact:
            return self.parent_contact.get_absolute_url()
        return super().get_success_url()


class DealUpdateView(StaffRequiredMixin, _DealFormMixin, UpdateView):
    model = Deal


class DealDeleteView(StaffRequiredMixin, DeleteView):
    model = Deal
    success_url = reverse_lazy("crm:deal_list")


# --- Quote -----------------------------------------------------------------
# Quotes are always managed in the context of a parent Deal — no list/detail.

class QuoteCreateView(StaffRequiredMixin, CreateView):
    """Create a quote. Requires `?deal=<pk>` — the deal is set from URL, not the form."""

    model = Quote
    form_class = QuoteForm

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
        if self.parent_deal.funded_amount is None:
            form.add_error(
                None,
                "This deal has no funded amount set, so we can't calculate a monthly payment. "
                "Edit the deal and set the funded amount first.",
            )
            return self.form_invalid(form)
        form.instance.deal = self.parent_deal
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["parent_deal"] = self.parent_deal
        return ctx

    def get_success_url(self):
        return self.parent_deal.get_absolute_url()


# --- Customer portal --------------------------------------------------------

class _PortalLinkMixin(StaffRequiredMixin):
    """Shared logic for minting a customer magic link from a Deal.

    Auto-creates (or reuses) the customer's User account, then issues a
    single-use MagicLink. Adds error/warning flash messages as needed.
    Returns (link, full_url) on success, or (None, None) on failure.
    """

    def issue_link(self, request, deal):
        contact = deal.customer

        if contact.user is not None:
            link_user = contact.user
        else:
            if not contact.email:
                messages.error(
                    request,
                    "Can't issue a link: this contact has no email address on file.",
                )
                return None, None
            User = get_user_model()
            # Reuse an existing user with that email if one already exists
            # (e.g. imported from HubSpot, or shared across contacts).
            link_user, was_created = User.objects.get_or_create(
                email=contact.email,
                defaults={
                    "role": Role.CUSTOMER,
                    "first_name": contact.first_name,
                    "last_name": contact.last_name,
                },
            )
            if was_created:
                link_user.set_unusable_password()
                link_user.save(update_fields=["password"])

            # Contact.user is OneToOne: don't try to relink if this user is
            # already attached to another contact. The link still works.
            other = Contact.objects.filter(user=link_user).exclude(pk=contact.pk).first()
            if other is not None:
                messages.warning(
                    request,
                    f"Heads up: the email {contact.email} is already linked to contact "
                    f"“{other}”. The link works fine, but this contact won't be "
                    f"re-linked to the user. Consider deduplicating the contacts.",
                )
            else:
                contact.user = link_user
                contact.save(update_fields=["user"])

        link = MagicLink.issue(
            user=link_user,
            redirect_url=reverse("crm:portal_quote_select", args=[deal.pk]),
            created_by=request.user,
        )
        full_url = request.build_absolute_uri(reverse("consume_magic_link", args=[link.token]))
        return link, full_url


class IssuePortalLinkView(_PortalLinkMixin, View):
    """Generate a portal link and show its URL to staff for manual sending."""

    def post(self, request, pk):
        deal = get_object_or_404(Deal.objects.select_related("customer"), pk=pk)
        link, full_url = self.issue_link(request, deal)
        if link is None:
            return redirect(deal.get_absolute_url())
        messages.success(
            request,
            f"Portal link generated for {deal.customer.email}. "
            f"Expires {link.expires_at:%d %b %Y, %H:%M}. URL: {full_url}",
        )
        return redirect(deal.get_absolute_url())


class EmailPortalLinkView(_PortalLinkMixin, View):
    """Generate a portal link and email it to the customer via the configured backend."""

    def post(self, request, pk):
        deal = get_object_or_404(Deal.objects.select_related("customer", "owner"), pk=pk)
        link, full_url = self.issue_link(request, deal)
        if link is None:
            return redirect(deal.get_absolute_url())

        from accounts.emails import send_magic_link_email
        send_magic_link_email(
            to_email=link.user.email,
            link_url=full_url,
            deal_name=deal.name,
            owner_name=deal.owner.full_name,
            expires_at=link.expires_at,
        )
        messages.success(
            request,
            f"Portal link emailed to {link.user.email}. "
            f"Expires {link.expires_at:%d %b %Y, %H:%M}.",
        )
        return redirect(deal.get_absolute_url())


class CustomerRequiredMixin(LoginRequiredMixin):
    """Mixin: 403 unless the user has the `customer` role."""

    def dispatch(self, request, *args, **kwargs):
        if not (request.user.is_authenticated and request.user.is_customer):
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


class _PortalDealMixin(CustomerRequiredMixin):
    """Shared queryset filter for portal views — only deals belonging to the user."""

    def get_deal(self, pk):
        return get_object_or_404(
            Deal.objects.filter(customer__user=self.request.user)
            .select_related("owner", "customer", "customer__organisation")
            .prefetch_related("quotes", "stage_events"),
            pk=pk,
        )


class PortalQuoteSelectView(_PortalDealMixin, View):
    """Step 1: customer picks a quote."""

    template_name = "crm/portal_quote_select.html"

    def get(self, request, pk):
        deal = self.get_deal(pk)
        form = QuoteSelectionForm(deal=deal, initial={"quote": deal.selected_quote_id})
        return self._render(request, deal, form)

    def post(self, request, pk):
        deal = self.get_deal(pk)
        form = QuoteSelectionForm(request.POST, deal=deal)
        if form.is_valid():
            deal.selected_quote = form.cleaned_data["quote"]
            deal.save(update_fields=["selected_quote"])
            return redirect("crm:portal_application", pk=deal.pk)
        return self._render(request, deal, form)

    def _render(self, request, deal, form):
        from django.shortcuts import render
        return render(request, self.template_name, {"deal": deal, "form": form})


class PortalApplicationView(_PortalDealMixin, View):
    """Step 2: customer confirms their info + adds co-applicants."""

    template_name = "crm/portal_application.html"

    def dispatch(self, request, *args, **kwargs):
        # Gate: must complete step 1 first
        if request.user.is_authenticated and request.user.is_customer:
            deal = Deal.objects.filter(customer__user=request.user, pk=kwargs["pk"]).first()
            if deal and not deal.selected_quote_id:
                return redirect("crm:portal_quote_select", pk=kwargs["pk"])
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, pk):
        deal = self.get_deal(pk)
        return self._render(request, deal, *self._build_forms(deal))

    def post(self, request, pk):
        deal = self.get_deal(pk)
        company_form, customer_form, co_formset = self._build_forms(deal, data=request.POST)
        all_valid = (
            company_form.is_valid()
            and customer_form.is_valid()
            and co_formset.is_valid()
        )
        if all_valid:
            company_form.save()
            customer_form.save()

            # Save co-applicants. Forms marked DELETE get unlinked (not deleted —
            # the underlying Contact may exist for other reasons).
            org = deal.customer.organisation
            active_contacts = []
            for form in co_formset:
                if form.cleaned_data.get("DELETE"):
                    continue
                if not form.has_changed() and form.instance.pk is None:
                    # Empty newly-added form, never touched — ignore
                    continue
                contact = form.save(commit=False)
                if contact.organisation_id is None:
                    contact.organisation = org
                contact.save()
                active_contacts.append(contact)
            deal.co_applicants.set(active_contacts)

            # Record an Info Received stage event automatically
            if (deal.current_stage is None) or (deal.current_stage.name != Stage.Name.INFO_RECEIVED):
                Stage.objects.create(
                    deal=deal,
                    name=Stage.Name.INFO_RECEIVED,
                    set_by=request.user,
                    note="Customer completed application via portal.",
                )

            return redirect("crm:portal_application_complete", pk=deal.pk)
        return self._render(request, deal, company_form, customer_form, co_formset)

    def _build_forms(self, deal, data=None):
        company_form = CompanyInfoForm(data, instance=deal.customer.organisation, prefix="company")
        customer_form = CustomerInfoForm(data, instance=deal.customer, prefix="customer")
        # Formset starts with whatever co-applicants are already linked.
        co_formset = CoApplicantFormSet(
            data,
            queryset=deal.co_applicants.all(),
            prefix="co",
        )
        return company_form, customer_form, co_formset

    def _render(self, request, deal, company_form, customer_form, co_formset):
        from django.shortcuts import render
        return render(
            request,
            self.template_name,
            {
                "deal": deal,
                "company_form": company_form,
                "customer_form": customer_form,
                "co_formset": co_formset,
            },
        )


class PortalApplicationCompleteView(_PortalDealMixin, View):
    """Step 3: thank-you page after the application is submitted."""

    def get(self, request, pk):
        from django.shortcuts import render
        deal = self.get_deal(pk)
        return render(request, "crm/portal_application_complete.html", {"deal": deal})


class QuoteUpdateView(StaffRequiredMixin, UpdateView):
    model = Quote
    form_class = QuoteForm

    def get_queryset(self):
        return super().get_queryset().select_related("deal")

    def form_valid(self, form):
        if form.instance.deal.funded_amount is None:
            form.add_error(
                None,
                "This deal has no funded amount set, so we can't recalculate the monthly payment. "
                "Edit the deal and set the funded amount first.",
            )
            return self.form_invalid(form)
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["parent_deal"] = self.object.deal
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


# --- Stage -----------------------------------------------------------------
# Stages are an immutable event log — only Create. To "undo" a stage change,
# just add another stage event with the previous value.

class StageCreateView(StaffRequiredMixin, CreateView):
    """Record a stage change. Requires `?deal=<pk>`."""

    model = Stage
    form_class = StageForm

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
        form.instance.set_by = self.request.user
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["parent_deal"] = self.parent_deal
        return ctx

    def get_success_url(self):
        return self.parent_deal.get_absolute_url()
