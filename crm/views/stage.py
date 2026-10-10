from django.views.generic import CreateView

from accounts.permissions import StaffRequiredMixin

from ..forms import StageForm
from ..models import Stage
from .shared import ParentDealMixin

# Stages are an immutable event log — only Create. To "undo" a stage change,
# just add another stage event with the previous value.

class StageCreateView(StaffRequiredMixin, ParentDealMixin, CreateView):
    """Record a stage change. Requires `?deal=<pk>`."""

    model = Stage
    form_class = StageForm

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
