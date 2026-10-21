"""Every transactional email carries the Medifinance logo as an inline (CID)
PNG, structured as multipart/related so clients render it in the header
rather than listing it as an attachment."""

from django.core import mail
from django.test import TestCase
from django.utils import timezone

from accounts.emails import LOGO_CID, send_magic_link_email


class EmailBrandingTests(TestCase):
    def test_logo_is_embedded_inline(self):
        send_magic_link_email(
            to_email="jane@example.com",
            link_url="https://app.example.com/p/abc",
            deal_name="Dental chair",
            owner_name="Matt",
            expires_at=timezone.now(),
        )

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        html = message.alternatives[0][0]
        self.assertIn(f'src="cid:{LOGO_CID}"', html)
        self.assertIn("Open my application", html)
        self.assertTrue(html.lstrip().startswith("<!doctype html>"), html[:200])

        mime = message.message()
        self.assertEqual(mime.get_content_type(), "multipart/related")
        images = [p for p in mime.walk() if p.get_content_type() == "image/png"]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["Content-ID"], f"<{LOGO_CID}>")
        self.assertTrue(images[0]["Content-Disposition"].startswith("inline"))
        self.assertGreater(len(images[0].get_payload(decode=True)), 1000)
