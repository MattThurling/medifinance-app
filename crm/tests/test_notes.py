"""Notes — display on detail pages and adding admin comments from the UI."""

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm.models import Note
from crm.templatetags.crm_extras import markdown

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


class NoteMarkdownTests(TestCase):
    """Note content is Markdown, rendered to sanitized HTML by the
    `markdown` filter (crm_extras)."""

    def test_markdown_link(self):
        html = markdown("See the [quote](https://example.com/q.pdf).")
        self.assertIn('<a href="https://example.com/q.pdf"', html)
        self.assertIn('class="link"', html)
        self.assertIn('target="_blank"', html)
        self.assertIn("rel=", html)

    def test_bare_url_is_auto_linked(self):
        html = markdown("Call notes: https://example.com/call")
        self.assertIn('<a href="https://example.com/call"', html)

    def test_newlines_become_breaks(self):
        self.assertIn("<br", markdown("line one\nline two"))

    def test_unsafe_html_is_stripped(self):
        html = markdown('<script>alert(1)</script> hi <b onclick="x">bold</b>')
        self.assertNotIn("<script", html)
        self.assertNotIn("onclick", html)
        self.assertIn("<b>bold</b>", html)

    def test_javascript_url_is_not_a_link(self):
        self.assertNotIn("<a", markdown("[click](javascript:alert(1))"))

    def test_note_link_renders_on_detail_page(self):
        staff = make_associate()
        deal = make_deal(owner=staff)
        Note.objects.create(
            type=Note.Type.HUBSPOT_EMAIL,
            deal=deal,
            content="Sent the [quote](https://example.com/q.pdf).",
            datetime=timezone.now(),
        )
        self.client.force_login(staff)
        response = self.client.get(deal.get_absolute_url())
        self.assertContains(response, '<a href="https://example.com/q.pdf"')


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
