"""Document upload + download permissions.

`_can_access_document` is the gate: staff see everything; a customer only sees
their own deal's documents. The download view streams through Django (the
storage bucket itself is private) so this gate is the entire access check."""

import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from crm.models import Document

from .factories import (
    make_associate,
    make_customer_with_deal,
    make_document,
)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class DocumentDownloadAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = make_associate()
        cls.customer_a, cls.deal_a = make_customer_with_deal(owner=cls.staff)
        cls.customer_b, cls.deal_b = make_customer_with_deal(owner=cls.staff)

        cls.doc_a = make_document(cls.deal_a, name="Bank statements")
        cls.doc_b = make_document(cls.deal_b, name="Bank statements")
        cls.doc_a.attach(SimpleUploadedFile("a.pdf", b"%PDF-A"), by=cls.staff)
        cls.doc_b.attach(SimpleUploadedFile("b.pdf", b"%PDF-B"), by=cls.staff)

    def _download_url(self, doc):
        return reverse("crm:document_download", args=[doc.pk])

    def test_customer_can_download_own_document(self):
        self.client.force_login(self.customer_a)
        response = self.client.get(self._download_url(self.doc_a))
        self.assertEqual(response.status_code, 200)
        body = b"".join(response.streaming_content)
        self.assertEqual(body, b"%PDF-A")

    def test_customer_cannot_download_other_customers_document(self):
        self.client.force_login(self.customer_a)
        response = self.client.get(self._download_url(self.doc_b))
        self.assertEqual(response.status_code, 403)

    def test_staff_can_download_any_document(self):
        self.client.force_login(self.staff)
        for doc in (self.doc_a, self.doc_b):
            with self.subTest(doc=doc.pk):
                response = self.client.get(self._download_url(doc))
                self.assertEqual(response.status_code, 200)

    def test_anonymous_redirected_to_login(self):
        """Download view uses LoginRequiredMixin (not StaffRequiredMixin), so
        anon DOES get the login redirect — unlike staff CRM URLs."""
        response = self.client.get(self._download_url(self.doc_a))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class DocumentUploadAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = make_associate()
        cls.customer_a, cls.deal_a = make_customer_with_deal(owner=cls.staff)
        cls.customer_b, cls.deal_b = make_customer_with_deal(owner=cls.staff)

    def setUp(self):
        # Build fresh blank documents per test so we can assert "no file attached".
        self.doc_a = make_document(self.deal_a, name="Bank statements")
        self.doc_b = make_document(self.deal_b, name="Bank statements")

    def _upload_url(self, doc):
        return reverse("crm:document_upload", args=[doc.pk])

    def _file(self, name="ok.pdf", body=b"%PDF-1.4"):
        return SimpleUploadedFile(name, body, content_type="application/pdf")

    def test_customer_can_upload_to_own_document(self):
        self.client.force_login(self.customer_a)
        response = self.client.post(
            self._upload_url(self.doc_a), {"file": self._file()},
        )
        self.assertEqual(response.status_code, 302)
        self.doc_a.refresh_from_db()
        self.assertTrue(self.doc_a.file)
        self.assertEqual(self.doc_a.status, Document.Status.PROVIDED)
        self.assertEqual(self.doc_a.uploaded_by, self.customer_a)

    def test_customer_cannot_upload_to_other_customers_document(self):
        self.client.force_login(self.customer_a)
        response = self.client.post(
            self._upload_url(self.doc_b), {"file": self._file(b"forgery")},
        )
        self.assertEqual(response.status_code, 403)
        self.doc_b.refresh_from_db()
        self.assertFalse(self.doc_b.file)
        self.assertEqual(self.doc_b.status, Document.Status.REQUESTED)

    def test_staff_can_upload_to_any_document(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            self._upload_url(self.doc_b), {"file": self._file()},
        )
        self.assertEqual(response.status_code, 302)
        self.doc_b.refresh_from_db()
        self.assertTrue(self.doc_b.file)

    def test_anonymous_upload_redirected_to_login(self):
        response = self.client.post(
            self._upload_url(self.doc_a), {"file": self._file()},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)
