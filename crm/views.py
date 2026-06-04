from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import ProtectedError, Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
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
    DocumentRequestForm,
    DocumentUploadForm,
    OrganisationForm,
    ParticipationForm,
    ProposalForm,
    SupplierInvoiceForm,
    QuoteForm,
    QuoteSelectionForm,
    StageForm,
)
from .models import (
    Contact,
    Deal,
    Document,
    Organisation,
    Participation,
    ParticipationInvoiceLink,
    Proposal,
    Quote,
    Stage,
)


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
    search_fields = [
        "name",
        "legal_name",
        "trading_name",
        "companies_house_number",
        "hubspot_id",
    ]


class OrganisationDetailView(StaffRequiredMixin, DetailView):
    model = Organisation

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["contacts"] = self.object.contacts.all()
        ctx["deals"] = Deal.objects.filter(organisation=self.object).select_related("owner", "customer")
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
    search_fields = ["first_name", "last_name", "email", "hubspot_id", "organisations__name"]

    def get_queryset(self):
        return super().get_queryset().prefetch_related("organisations")


class ContactDetailView(StaffRequiredMixin, DetailView):
    model = Contact

    def get_queryset(self):
        return super().get_queryset().select_related("user").prefetch_related("organisations")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["deals"] = self.object.deals.select_related("owner").all()
        return ctx


class ContactCreateView(StaffRequiredMixin, CreateView):
    """Create a contact. Accepts `?organisation=<pk>` to prefill + lock the org FK
    (from an organisation's detail page), or `?deal=<pk>` to attach the new contact
    to that deal as a co-applicant (org locked to the deal's customer's org)."""

    model = Contact
    form_class = ContactForm

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.parent_organisation = None
        self.parent_deal = None

        deal_pk = request.GET.get("deal")
        if deal_pk:
            try:
                self.parent_deal = Deal.objects.select_related("organisation").get(pk=deal_pk)
                self.parent_organisation = self.parent_deal.organisation
            except (Deal.DoesNotExist, ValueError, TypeError):
                pass

        if self.parent_organisation is None:
            org_pk = request.GET.get("organisation")
            if org_pk:
                try:
                    self.parent_organisation = Organisation.objects.get(pk=org_pk)
                except (Organisation.DoesNotExist, ValueError, TypeError):
                    pass

    def get_initial(self):
        initial = super().get_initial()
        if self.parent_organisation:
            initial["organisations"] = [self.parent_organisation]
        return initial

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["parent_organisation"] = self.parent_organisation
        ctx["parent_deal"] = self.parent_deal
        return ctx

    def form_valid(self, form):
        response = super().form_valid(form)  # saves contact + organisations M2M
        if self.parent_deal:
            self.parent_deal.co_applicants.add(self.object)
            if self.parent_deal.organisation_id:
                # Idempotent; covers the locked-org case even when the M2M field
                # wasn't submitted (e.g. if the template renders a hidden input).
                self.object.organisations.add(self.parent_deal.organisation_id)
            messages.success(self.request, f"Added {self.object} as an applicant.")
        return response

    def get_success_url(self):
        if self.parent_deal:
            return self.parent_deal.get_absolute_url()
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
        "organisation__name",
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
            .select_related("owner", "customer", "organisation")
            .annotate(current_stage_name=Subquery(latest_stage))
        )


class DealDetailView(StaffRequiredMixin, DetailView):
    model = Deal

    def get_queryset(self):
        return super().get_queryset().select_related(
            "owner",
            "customer",
            "organisation",
            "introducer",
        ).prefetch_related(
            "customer__organisations",
            "introducer__organisations",
            "quotes",
            "stage_events",
            "co_applicants",
            "documents",
            "participations__organisation",
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        deal = self.object

        # Stage history oldest-first for the vertical steps timeline
        ctx["stages_chrono"] = (
            deal.stage_events.select_related("organisation").order_by("occurred_at", "pk")
        )

        # Applicants = the lead (customer) plus any co-applicants
        applicants = [deal.customer, *deal.co_applicants.all()]
        ctx["applicants"] = applicants

        # Other contacts in the deal's organisation that staff can attach as applicants
        org = deal.organisation
        ctx["available_applicants"] = (
            org.contacts.exclude(pk__in=[a.pk for a in applicants])
            if org else Contact.objects.none()
        )

        # Split the amount so the pence can render smaller (£25,000.00)
        if deal.funded_amount is not None:
            pounds = int(deal.funded_amount)
            pence = int((deal.funded_amount - pounds) * 100)
            ctx["amount_pounds"] = f"{pounds:,}"
            ctx["amount_pence"] = f"{pence:02d}"

        if deal.commission is not None:
            ctx["commission_display"] = f"£{deal.commission:,.2f}"
        return ctx


class _DealFormMixin:
    form_class = DealForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["current_user"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        owner_id = ctx["form"]["owner"].value()
        ctx["owner_selected_id"] = owner_id or ""
        ctx["owner_selected_label"] = (
            get_user_model().objects.filter(pk=owner_id).first() if owner_id else ""
        )
        return ctx

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


class DealApplicantAddView(StaffRequiredMixin, View):
    """Attach an existing contact from the customer's organisation as a co-applicant."""

    def post(self, request, pk):
        deal = get_object_or_404(Deal.objects.select_related("organisation"), pk=pk)
        contact = get_object_or_404(Contact, pk=request.POST.get("contact"))
        if deal.organisation_id is None:
            messages.error(request, "Set the deal's organisation before adding applicants.")
        elif not contact.organisations.filter(pk=deal.organisation_id).exists():
            messages.error(request, "You can only add applicants from the deal's organisation.")
        elif contact.pk == deal.customer_id:
            messages.error(request, "That contact is already the lead applicant.")
        else:
            deal.co_applicants.add(contact)
            messages.success(request, f"Added {contact} as an applicant.")
        return redirect(deal.get_absolute_url())


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
        if self.parent_deal.funded_amount is not None:
            ctx["funded_amount_display"] = f"£{self.parent_deal.funded_amount:,.2f}"
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


class _SearchView(StaffRequiredMixin, View):
    """Base for HTMX combobox search endpoints. Returns an HTML fragment of
    up to `limit` matches rendered as clickable options."""

    model = None
    search_fields: list[str] = []
    limit = 20

    def get_queryset(self):
        return self.model.objects.all()

    def get(self, request):
        from django.shortcuts import render

        q = request.GET.get("q", "").strip()
        results = []
        if q:
            cond = Q()
            for field in self.search_fields:
                cond |= Q(**{f"{field}__icontains": q})
            results = list(self.get_queryset().filter(cond).distinct()[: self.limit])
        return render(request, "crm/_combobox_results.html", {"results": results})


class ContactSearchView(_SearchView):
    model = Contact
    search_fields = ["first_name", "last_name", "email", "organisations__name"]

    def get_queryset(self):
        return Contact.objects.prefetch_related("organisations")


class OrganisationSearchView(_SearchView):
    model = Organisation
    search_fields = ["name", "legal_name", "trading_name", "companies_house_number"]


class UserSearchView(_SearchView):
    """Search staff users (admins + associates) for the deal owner combobox."""

    search_fields = ["first_name", "last_name", "email"]

    def get_queryset(self):
        return get_user_model().objects.filter(role__in=[Role.ADMIN, Role.ASSOCIATE])


class _PortalDealMixin(CustomerRequiredMixin):
    """Shared queryset filter for portal views — only deals belonging to the user."""

    def get_deal(self, pk):
        return get_object_or_404(
            Deal.objects.filter(customer__user=self.request.user)
            .select_related("owner", "customer", "organisation")
            .prefetch_related("quotes", "stage_events"),
            pk=pk,
        )


class _PortalStepMixin(_PortalDealMixin):
    """A step in the customer application wizard. Steps after Quotes require a
    quote to have been selected; otherwise we bounce back to the Quotes step."""

    step = 1

    def dispatch(self, request, *args, **kwargs):
        if self.step > 1 and request.user.is_authenticated and getattr(request.user, "is_customer", False):
            deal = Deal.objects.filter(customer__user=request.user, pk=kwargs["pk"]).first()
            if deal and not deal.selected_quote_id:
                return redirect("crm:portal_quote_select", pk=kwargs["pk"])
        return super().dispatch(request, *args, **kwargs)


class PortalQuoteSelectView(_PortalStepMixin, View):
    """Step 1: customer picks a quote."""

    step = 1
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
            return redirect("crm:portal_company", pk=deal.pk)
        return self._render(request, deal, form)

    def _render(self, request, deal, form):
        from django.shortcuts import render
        amount_display = f"£{deal.funded_amount:,.2f}" if deal.funded_amount is not None else None
        return render(
            request,
            self.template_name,
            {"deal": deal, "form": form, "amount_display": amount_display, "current_step": self.step},
        )


class PortalCompanyView(_PortalStepMixin, View):
    """Step 2: company details (organisation address)."""

    step = 2
    template_name = "crm/portal_company.html"

    def get(self, request, pk):
        deal = self.get_deal(pk)
        form = CompanyInfoForm(instance=deal.organisation, prefix="company")
        return self._render(request, deal, form)

    def post(self, request, pk):
        deal = self.get_deal(pk)
        form = CompanyInfoForm(request.POST, instance=deal.organisation, prefix="company")
        if form.is_valid():
            org = form.save()
            # Link the saved org to the deal (if not already) and to the customer's
            # organisations M2M (add is idempotent).
            if deal.organisation_id != org.pk:
                deal.organisation = org
                deal.save(update_fields=["organisation"])
            deal.customer.organisations.add(org)
            return redirect("crm:portal_applicants", pk=deal.pk)
        return self._render(request, deal, form)

    def _render(self, request, deal, form):
        from django.shortcuts import render
        return render(
            request,
            self.template_name,
            {"deal": deal, "form": form, "current_step": self.step},
        )


class PortalApplicantsView(_PortalStepMixin, View):
    """Step 3: the lead's details + any co-applicants. Records 'Info Received'."""

    step = 3
    template_name = "crm/portal_applicants.html"

    def get(self, request, pk):
        deal = self.get_deal(pk)
        return self._render(request, deal, *self._build_forms(deal))

    def post(self, request, pk):
        deal = self.get_deal(pk)
        customer_form, co_formset = self._build_forms(deal, data=request.POST)
        if customer_form.is_valid() and co_formset.is_valid():
            customer_form.save()

            # Save co-applicants. Forms marked DELETE get unlinked (not deleted).
            # Newly-saved contacts get the deal's organisation added to their M2M
            # so they're discoverable under that org going forward.
            org = deal.organisation
            active_contacts = []
            for form in co_formset:
                if form.cleaned_data.get("DELETE"):
                    continue
                if not form.has_changed() and form.instance.pk is None:
                    continue
                contact = form.save(commit=False)
                is_new = contact.pk is None
                contact.save()
                if is_new and org is not None:
                    contact.organisations.add(org)
                active_contacts.append(contact)
            deal.co_applicants.set(active_contacts)

            # Record an Info Received stage event automatically (once).
            if (deal.current_stage is None) or (deal.current_stage.name != Stage.Name.INFO_RECEIVED):
                Stage.objects.create(
                    deal=deal,
                    name=Stage.Name.INFO_RECEIVED,
                    organisation=deal.organisation,
                    set_by=request.user,
                    note="Customer completed application via portal.",
                )

            return redirect("crm:portal_documents", pk=deal.pk)
        return self._render(request, deal, customer_form, co_formset)

    def _build_forms(self, deal, data=None):
        customer_form = CustomerInfoForm(data, instance=deal.customer, prefix="customer")
        co_formset = CoApplicantFormSet(data, queryset=deal.co_applicants.all(), prefix="co")
        return customer_form, co_formset

    def _render(self, request, deal, customer_form, co_formset):
        from django.shortcuts import render
        return render(
            request,
            self.template_name,
            {
                "deal": deal,
                "customer_form": customer_form,
                "co_formset": co_formset,
                "current_step": self.step,
            },
        )


class PortalApplicationCompleteView(_PortalStepMixin, View):
    """Final thank-you page after the wizard is complete."""

    step = 4

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
        # Manual stages default to the deal's organisation (the client) — staff
        # can add more granular stages via the auto-fired events on Proposals
        # and Participations instead.
        form.instance.organisation = self.parent_deal.organisation
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["parent_deal"] = self.parent_deal
        return ctx

    def get_success_url(self):
        return self.parent_deal.get_absolute_url()


# --- Proposal --------------------------------------------------------------
# Proposals are managed in the context of a parent Deal (the broker shops the
# deal to many lenders), same shape as Quote — no separate list/detail page.

class ProposalCreateView(StaffRequiredMixin, CreateView):
    """Create a proposal. Requires `?deal=<pk>` — the deal is set from URL, not the form."""

    model = Proposal
    form_class = ProposalForm

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
        response = super().form_valid(form)
        Stage.objects.create(
            deal=self.parent_deal,
            name=Stage.Name.PROPOSAL_SUBMITTED,
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


class ProposalUpdateView(StaffRequiredMixin, UpdateView):
    model = Proposal
    form_class = ProposalForm

    # Map a new Proposal.Status -> the Stage.Name to emit when status flips to it.
    _STATUS_TO_STAGE = {
        Proposal.Status.SUBMITTED: Stage.Name.PROPOSAL_SUBMITTED,
        Proposal.Status.APPROVED: Stage.Name.PROPOSAL_APPROVED,
        Proposal.Status.DECLINED: Stage.Name.PROPOSAL_DECLINED,
        Proposal.Status.WITHDRAWN: Stage.Name.PROPOSAL_WITHDRAWN,
    }

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
            stage_name = self._STATUS_TO_STAGE.get(new_status)
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


# --- Participation (Supplier) ----------------------------------------------
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
    invoice contact. Requires an approved Proposal on the deal so the email body
    can name the accepted lender."""

    def post(self, request, pk):
        participation = get_object_or_404(
            Participation.objects.select_related(
                "deal", "deal__organisation", "deal__customer",
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

        approved = participation.deal.proposals.filter(
            status=Proposal.Status.APPROVED
        ).select_related("lender").first()
        if approved is None:
            messages.error(
                request,
                "Mark a Proposal as Approved before sending an invoice request — "
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
            client_org_name=client_org.name,
            client_org_address=client_org.display_address,
            lender_org_name=approved.lender.name,
            lender_org_address=approved.lender.display_address,
            participation_amount_display=f"£{participation.amount:,.2f}",
            participation_description=(participation.description or "").strip(),
            expires_at=link.expires_at,
        )

        Stage.objects.create(
            deal=participation.deal,
            name=Stage.Name.INVOICE_REQUESTED,
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
            Stage.objects.create(
                deal=link.participation.deal,
                name=Stage.Name.INVOICE_RECEIVED,
                organisation=link.participation.organisation,
                # No set_by — the supplier isn't a logged-in user.
            )
            return render(request, self.complete_template_name,
                          {"participation": link.participation})
        return render(request, self.template_name,
                      {"form": form, "link": link, "participation": link.participation})


# --- Documents -------------------------------------------------------------

def _can_access_document(user, doc: Document) -> bool:
    """Staff see any document; a customer only their own deal's."""
    if not user.is_authenticated:
        return False
    if user.is_admin or user.is_associate:
        return True
    return user.is_customer and doc.deal.customer.user_id == user.id


class DocumentCreateView(StaffRequiredMixin, CreateView):
    """Staff: add a document request to a deal (file uploaded later)."""

    model = Document
    form_class = DocumentRequestForm

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

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["parent_deal"] = self.parent_deal
        return ctx

    def get_success_url(self):
        return self.parent_deal.get_absolute_url()


class DocumentDeleteView(StaffRequiredMixin, DeleteView):
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

        form = DocumentUploadForm(request.POST, request.FILES, instance=doc)
        if form.is_valid():
            doc.attach(form.cleaned_data["file"], by=request.user)
            messages.success(request, f"“{doc.name}” uploaded.")
        else:
            messages.error(request, f"Couldn't upload “{doc.name}”. Please choose a file.")

        if request.user.is_customer:
            return redirect("crm:portal_documents", pk=doc.deal_id)
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
        return FileResponse(doc.file.open("rb"), as_attachment=True, filename=filename)


class PortalDocumentsView(_PortalStepMixin, View):
    """Step 4: customer sees required documents for their deal and uploads them."""

    step = 4
    template_name = "crm/portal_documents.html"

    def get(self, request, pk):
        deal = self.get_deal(pk)
        return render(
            request,
            self.template_name,
            {
                "deal": deal,
                "documents": deal.documents.all(),
                "upload_form": DocumentUploadForm(),
                "current_step": self.step,
            },
        )
