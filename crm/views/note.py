from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.views import View

from accounts.permissions import StaffRequiredMixin

from ..access import visible_contacts, visible_deals, visible_organisations
from ..models import Note


class NoteCreateView(StaffRequiredMixin, View):
    """Add a note from a contact / organisation / deal detail page. UI-added
    notes are always admin comments by the logged-in user, written now."""

    # Each parent resolves to the rows this user may see (crm.access).
    parents = {
        "contact": visible_contacts,
        "organisation": visible_organisations,
        "deal": visible_deals,
    }

    def post(self, request):
        given = [f for f in self.parents if request.POST.get(f)]
        if len(given) != 1:
            raise Http404("Provide exactly one of contact, organisation or deal.")
        field = given[0]
        parent = get_object_or_404(self.parents[field](request.user), pk=request.POST[field])

        content = request.POST.get("content", "").strip()
        if content:
            from django.utils import timezone

            note = Note(
                type=Note.Type.ADMIN_COMMENT,
                author=request.user,
                content=content,
                datetime=timezone.now(),
                **{field: parent},
            )
            note.save()
            # `datetime` means "when written" — for UI notes that's the moment
            # of creation, so mirror the auto_now_add stamp exactly.
            note.datetime = note.created_at
            note.save(update_fields=["datetime"])
        else:
            messages.error(request, "Note can't be empty.")
        return redirect(parent.get_absolute_url())
