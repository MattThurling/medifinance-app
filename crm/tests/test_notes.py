"""Notes — display on detail pages and adding admin comments from the UI."""

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm.models import Note

from .factories import make_associate, make_contact, make_deal, make_organisation


class NoteDisplayTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = make_associate()
        cls.deal = make_deal(owner=cls.staff)
        cls.contact = cls.deal.customer
        cls.organisation = cls.deal.organisation

    def setUp(self):
        self.client.force_login(self.staff)

    def test_notes_render_on_each_detail_page(self):
        for parent_field, obj in [
            ("contact", self.contact),
            ("organisation", self.organisation),
            ("deal", self.deal),
        ]:
            Note.objects.create(
                type=Note.Type.HUBSPOT_NOTE,
                content=f"Imported note about the {parent_field}.",
                datetime=timezone.now(),
                **{parent_field: obj},
            )
            response = self.client.get(obj.get_absolute_url())
            self.assertContains(response, f"Imported note about the {parent_field}.")


class NoteCreateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = make_associate()
        cls.deal = make_deal(owner=cls.staff)

    def setUp(self):
        self.client.force_login(self.staff)

    def _add_note(self, **data):
        return self.client.post(reverse("crm:note_create"), data)

    def test_add_note_to_each_parent_type(self):
        for parent_field, obj in [
            ("contact", self.deal.customer),
            ("organisation", self.deal.organisation),
            ("deal", self.deal),
        ]:
            with self.subTest(parent=parent_field):
                response = self._add_note(**{parent_field: obj.pk, "content": "Called them."})
                self.assertRedirects(response, obj.get_absolute_url())
                note = obj.notes.get()
                self.assertEqual(note.type, Note.Type.ADMIN_COMMENT)
                self.assertEqual(note.author, self.staff)
                self.assertEqual(note.content, "Called them.")
                self.assertEqual(note.datetime, note.created_at)

    def test_blank_note_is_rejected(self):
        response = self._add_note(deal=self.deal.pk, content="   ")
        self.assertRedirects(response, self.deal.get_absolute_url())
        self.assertEqual(Note.objects.count(), 0)

    def test_requires_exactly_one_parent(self):
        response = self._add_note(content="Orphan note.")
        self.assertEqual(response.status_code, 404)
        response = self._add_note(
            deal=self.deal.pk, contact=self.deal.customer.pk, content="Two parents."
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(Note.objects.count(), 0)

    def test_anonymous_cannot_add_notes(self):
        self.client.logout()
        response = self._add_note(deal=self.deal.pk, content="Sneaky.")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Note.objects.count(), 0)
