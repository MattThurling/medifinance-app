from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.views import View

from accounts.permissions import StaffRequiredMixin

from ..access import visible_deals
from ..models import Contact, Deal, Note, Organisation


class NoteCreateView(StaffRequiredMixin, View):
    """Add a note from a contact / organisation / deal detail page. UI-added
    notes are always admin comments by the logged-in user, written now."""

    parents = {"contact": Contact, "organisation": Organisation, "deal": Deal}

    def post(self, request):
        given = [f for f in self.parents if request.POST.get(f)]
        if len(given) != 1:
            raise Http404("Provide exactly one of contact, organisation or deal.")
        field = given[0]
        # Contacts and orgs are a shared address book; deals are scoped.
        qs = visible_deals(request.user) if field == "deal" else self.parents[field].objects.all()
        parent = get_object_or_404(qs, pk=request.POST[field])

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
