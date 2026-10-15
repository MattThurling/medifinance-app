from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import OuterRef, ProtectedError, Q, Subquery, Sum
from django.db.models.functions import Coalesce, Greatest
from django.http import Http404
from django.shortcuts import redirect
from django.views import View
from django.views.generic import ListView

from accounts.models import Role
from accounts.permissions import StaffRequiredMixin

from ..access import get_visible_deal_or_404, owner_choices, scope_to_visible_deals, visible_deals
from ..models import (
    Document,
    Note,
    Organisation,
    Participation,
    Proposal,
    Quote,
    Stage,
    XeroInvoice,
)


def _latest(model, ts_field, **filters):
    """Subquery for the newest `ts_field` of a deal's related `model` rows."""
    return Subquery(
        model.objects.filter(deal=OuterRef("pk"), **filters)
        .order_by(f"-{ts_field}")
        .values(ts_field)[:1]
    )


def _deal_summaries(qs):
    """Annotate deals with their current stage code, funded total and latest
    activity so list pages don't have to fire one query per row for the Deal
    properties."""
    latest_stage = (
        Stage.objects.filter(deal=OuterRef("pk")).order_by("-occurred_at", "-pk").values("name")[:1]
    )
    funded = (
        Participation.objects.filter(deal=OuterRef("pk"))
        .values("deal")
        .annotate(total=Sum("amount"))
        .values("total")[:1]
    )
    return qs.annotate(
        current_stage_name=Subquery(latest_stage),
        funded_total=Subquery(funded),
        last_stage_at=_latest(Stage, "occurred_at"),
        last_note_at=_latest(Note, "datetime"),
        last_participation_at=_latest(Participation, "created_at"),
        last_proposal_at=_latest(Proposal, "created_at"),
        last_quote_at=_latest(Quote, "created_at"),
        last_document_at=_latest(
            Document, "uploaded_at", status=Document.Status.PROVIDED, uploaded_at__isnull=False
        ),
        last_xero_at=_latest(XeroInvoice, "created_at"),
    ).annotate(
        # On SQLite, Greatest returns NULL if *any* argument is NULL, so each
        # source is coalesced to created_at — which is also the fallback we
        # want for deals with no activity yet.
        last_activity_at=Greatest(
            Coalesce("last_stage_at", "created_at"),
            Coalesce("last_note_at", "created_at"),
            Coalesce("last_participation_at", "created_at"),
            Coalesce("last_proposal_at", "created_at"),
            Coalesce("last_quote_at", "created_at"),
            Coalesce("last_document_at", "created_at"),
            Coalesce("last_xero_at", "created_at"),
        ),
    )


# Priority order for label ties — a stage change often creates sibling records
# in the same instant, so it wins; the stage label itself is derived per row.
_ACTIVITY_LABELS = [
    ("last_stage_at", None),
    ("last_note_at", "Note added"),
    ("last_document_at", "Document uploaded"),
    ("last_participation_at", "Supplier invoice added"),
    ("last_proposal_at", "Proposal added"),
    ("last_quote_at", "Quote added"),
    # Labelled without the word "Xero" — the integration is hidden from
    # non-finance staff, but the recency signal itself stays visible to all.
    ("last_xero_at", "Invoice raised"),
]


def _attach_activity_labels(deals):
    """Set `last_activity_label` on each deal from `_deal_summaries` annotations."""
    for d in deals:
        d.last_activity_label = "Deal created"
        for attr, label in _ACTIVITY_LABELS:
            if d.last_activity_at is not None and getattr(d, attr) == d.last_activity_at:
                if label is None:
                    try:
                        stage = Stage.Name(d.current_stage_name).label
                    except ValueError:
                        stage = d.current_stage_name or ""
                    label = f"Stage: {stage}" if stage else "Stage changed"
                d.last_activity_label = label
                break


class SearchableListView(ListView):
    """ListView that supports `?q=` filtering via `search_fields`."""

    paginate_by = 25
    search_fields: list[str] = []

    def get_base_queryset(self):
        """The rows to search over. Override to scope by user."""
        return super().get_queryset()

    def get_queryset(self):
        qs = self.get_base_queryset()
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


class SortableListMixin:
    """Whitelist-based `?sort=` handling. `sort_fields` maps keys to ORM
    expressions; prefix the query value with '-' for descending. Views call
    `apply_sort(qs)` explicitly so ordering happens after any annotations."""

    sort_fields: dict = {}
    default_sort = ""

    def get_sort(self) -> str:
        sort = self.request.GET.get("sort", "")
        if sort.lstrip("-") not in self.sort_fields:
            return self.default_sort
        return sort

    def apply_sort(self, qs):
        sort = self.get_sort()
        if not sort:
            return qs
        expr = self.sort_fields[sort.lstrip("-")]
        if sort.startswith("-"):
            ordering = expr.desc(nulls_last=True)
        else:
            ordering = expr.asc(nulls_last=True)
        return qs.order_by(ordering, "-created_at", "-pk")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["sort"] = self.get_sort()
        return ctx


class VisibleDealsMixin:
    """For Deal CBVs: the queryset is the deals this user may see, so a deal
    belonging to someone else 404s like a missing one. Put it after the
    role-check mixin in the bases."""

    def get_queryset(self):
        return visible_deals(self.request.user)


class VisibleDealChildMixin:
    """For CBVs over a deal's children (Quote, Proposal, Participation,
    Document ...): restrict `super().get_queryset()` to visible deals."""

    def get_queryset(self):
        return scope_to_visible_deals(super().get_queryset(), self.request.user)


class ParentDealMixin:
    """Resolve the parent deal into `self.parent_deal` (404 if the user can't
    see it). Reads `?deal=<pk>` by default, or the URL `pk` kwarg when
    `parent_deal_from_url` is set.

    Runs in `dispatch()`, so it MUST come after `StaffRequiredMixin` in the
    bases: the role check (403) has to happen before the lookup (404),
    otherwise anonymous users could probe which deal ids exist."""

    parent_deal_from_url = False
    parent_deal_select_related: tuple[str, ...] = ()

    def dispatch(self, request, *args, **kwargs):
        pk = kwargs.get("pk") if self.parent_deal_from_url else request.GET.get("deal")
        if not pk:
            raise Http404("?deal query parameter required")
        try:
            self.parent_deal = get_visible_deal_or_404(
                request.user, pk, *self.parent_deal_select_related
            )
        except (ValueError, TypeError) as exc:
            raise Http404("Deal not found") from exc
        return super().dispatch(request, *args, **kwargs)


class OwnerFilterMixin:
    """Whitelist-based `?owner=` filtering shared by the deal, contact and
    organisation lists: `me`, `none`, or a user pk. Views call
    `apply_owner_filter(qs)` explicitly, alongside their other filters.

    Deal-based views set `lock_owner_for_associates = True`. For an associate
    with no organisation the queryset is already just their own deals, so the
    filter becomes a no-op and the dropdown is hidden (`owner_filter_locked`
    in context). An associate linked to an organisation shares deals with
    their colleagues, so they keep the dropdown — limited to that
    organisation's staff (`owner_choices`). Contact and organisation lists
    leave the lock off — the address book is shared, and "My contacts" is a
    legitimate filter there."""

    lock_owner_for_associates = False

    @property
    def owner_filter_locked(self) -> bool:
        user = self.request.user
        return self.lock_owner_for_associates and user.is_associate and not user.organisation_id

    def apply_owner_filter(self, qs):
        if self.owner_filter_locked:
            return qs
        owner = self.request.GET.get("owner", "")
        if owner == "me":
            qs = qs.filter(owner=self.request.user)
        elif owner == "none":
            qs = qs.filter(owner__isnull=True)
        elif owner.isdigit():
            qs = qs.filter(owner_id=owner)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        locked = self.owner_filter_locked
        ctx["owner_filter_locked"] = locked
        if locked:
            # Keep colleagues' names out of the page entirely.
            ctx["owner_choices"] = []
            ctx["owner_filter"] = "me"
        else:
            if self.lock_owner_for_associates:
                # Deal views: only the owners whose deals this user can see.
                choices = owner_choices(self.request.user)
            else:
                # Shared address book: any staff member is a valid filter.
                choices = get_user_model().objects.filter(
                    role__in=[Role.ADMIN, Role.ASSOCIATE]
                )
            ctx["owner_choices"] = (
                choices.filter(is_active=True).order_by("first_name", "last_name")
            )
            ctx["owner_filter"] = self.request.GET.get("owner", "")
        return ctx


class SectorFilterMixin:
    """Whitelist-based `?sector=` filtering: a Sector code or `none`. Views
    call `apply_sector_filter(qs)` explicitly, alongside their other filters."""

    def apply_sector_filter(self, qs):
        sector = self.request.GET.get("sector", "")
        if sector == "none":
            qs = qs.filter(Q(sector__isnull=True) | Q(sector=""))
        elif sector in Organisation.Sector.values:
            qs = qs.filter(sector=sector)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["sector_choices"] = Organisation.Sector.choices
        ctx["sector_filter"] = self.request.GET.get("sector", "")
        return ctx


class _OwnerFormMixin:
    """Passes the logged-in user to the form (owner default on create) and
    supplies the owner-combobox selection context."""

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["current_user"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        form = ctx["form"]
        if "owner" not in form.fields:
            # Associates don't get to pick an owner (OwnerInitialFormMixin
            # removes the field); the template renders nothing for it.
            ctx["owner_locked"] = True
            ctx["owner_selected_id"] = ""
            ctx["owner_selected_label"] = ""
            return ctx
        ctx["owner_locked"] = False
        owner_id = form["owner"].value()
        ctx["owner_selected_id"] = owner_id or ""
        ctx["owner_selected_label"] = (
            get_user_model().objects.filter(pk=owner_id).first() if owner_id else ""
        )
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
