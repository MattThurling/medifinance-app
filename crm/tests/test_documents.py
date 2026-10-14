"""Document upload + download permissions.

`_can_access_document` is the gate: staff see everything; a customer only sees
their own deal's documents. The download view streams through Django (the
storage bucket itself is private) so this gate is the entire access check."""

import tempfile
import zipfile

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


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class MultiFileUploadTests(TestCase):
    """Several files picked at once are stored as one zip; a single file is
    stored as-is so PDFs keep opening inline."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = make_associate()
        cls.customer, cls.deal = make_customer_with_deal(owner=cls.staff)

    def setUp(self):
        self.doc = make_document(self.deal, name="Last 3 months bank statements")
        self.client.force_login(self.customer)
        self.url = reverse("crm:document_upload", args=[self.doc.pk])

    def _pdf(self, name, body):
        return SimpleUploadedFile(name, body, content_type="application/pdf")

    def test_three_files_are_zipped(self):
        files = [self._pdf("jan.pdf", b"%PDF-jan"), self._pdf("feb.pdf", b"%PDF-feb"), self._pdf("mar.pdf", b"%PDF-mar")]
        response = self.client.post(self.url, {"file": files})
        self.assertEqual(response.status_code, 302)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.status, Document.Status.PROVIDED)
        stored = self.doc.file.name.rsplit("/", 1)[-1]
        self.assertTrue(stored.startswith("last-3-months-bank-statements"), stored)
        self.assertTrue(stored.endswith(".zip"), stored)
        with self.doc.file.open("rb") as fh, zipfile.ZipFile(fh) as archive:
            self.assertEqual(archive.namelist(), ["jan.pdf", "feb.pdf", "mar.pdf"])
            self.assertEqual(archive.read("feb.pdf"), b"%PDF-feb")

    def test_single_file_is_not_zipped(self):
        response = self.client.post(self.url, {"file": self._pdf("only.pdf", b"%PDF-1")})
        self.assertEqual(response.status_code, 302)
        self.doc.refresh_from_db()
        self.assertTrue(self.doc.file.name.endswith(".pdf"), self.doc.file.name)
        with self.doc.file.open("rb") as fh:
            self.assertEqual(fh.read(), b"%PDF-1")

    def test_duplicate_names_are_suffixed_inside_zip(self):
        files = [self._pdf("statement.pdf", b"a"), self._pdf("statement.pdf", b"b"), self._pdf("notes", b"c")]
        self.client.post(self.url, {"file": files})
        self.doc.refresh_from_db()
        with self.doc.file.open("rb") as fh, zipfile.ZipFile(fh) as archive:
            self.assertEqual(archive.namelist(), ["statement.pdf", "statement (2).pdf", "notes"])
            self.assertEqual(archive.read("statement (2).pdf"), b"b")

    def test_zip_downloads_as_attachment_but_pdf_opens_inline(self):
        self.client.post(self.url, {"file": [self._pdf("a.pdf", b"a"), self._pdf("b.pdf", b"b")]})
        response = self.client.get(reverse("crm:document_download", args=[self.doc.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Disposition"].startswith("attachment"))

        single = make_document(self.deal, name="Photo ID")
        single.attach(self._pdf("id.pdf", b"%PDF"), by=self.customer)
        response = self.client.get(reverse("crm:document_download", args=[single.pk]))
        self.assertTrue(response["Content-Disposition"].startswith("inline"))


@override_settings(
    MEDIA_ROOT=tempfile.mkdtemp(),
    DOCUMENT_UPLOAD_MAX_FILE_MB=1,
    DOCUMENT_UPLOAD_MAX_TOTAL_MB=2,
)
class UploadSizeLimitTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer, cls.deal = make_customer_with_deal()

    def setUp(self):
        self.doc = make_document(self.deal, name="Bank statements")
        self.client.force_login(self.customer)
        self.url = reverse("crm:document_upload", args=[self.doc.pk])

    def _pdf(self, name, size):
        return SimpleUploadedFile(name, b"x" * size, content_type="application/pdf")

    def test_file_over_per_file_limit_is_rejected_with_reason(self):
        response = self.client.post(
            self.url, {"file": self._pdf("big.pdf", 1024 * 1024 + 1)}, HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("“big.pdf” is too large. Each file must be under 1 MB.", response.content.decode())
        self.doc.refresh_from_db()
        self.assertFalse(self.doc.is_provided)

    def test_files_over_total_limit_are_rejected(self):
        files = [self._pdf(f"{n}.pdf", 900 * 1024) for n in range(3)]  # 2.6 MB, each under 1 MB
        response = self.client.post(self.url, {"file": files})
        self.assertEqual(response.status_code, 302)
        self.doc.refresh_from_db()
        self.assertFalse(self.doc.is_provided)
        messages = [m.message for m in response.wsgi_request._messages]
        self.assertTrue(any("more than 2 MB" in m for m in messages), messages)

    def test_files_within_limits_are_accepted(self):
        files = [self._pdf(f"{n}.pdf", 500 * 1024) for n in range(3)]  # 1.5 MB total
        self.client.post(self.url, {"file": files})
        self.doc.refresh_from_db()
        self.assertTrue(self.doc.is_provided)
