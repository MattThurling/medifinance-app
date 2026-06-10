from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import MagicLink, User


class MagicLinkModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="m@example.com", password="x")

    def test_issue_produces_valid_link(self):
        link = MagicLink.issue(user=self.user, redirect_url="/portal/deals/1/")
        self.assertTrue(link.token)
        self.assertFalse(link.is_consumed)
        self.assertFalse(link.is_expired)
        self.assertTrue(link.is_valid)

    def test_two_issued_links_have_distinct_tokens(self):
        a = MagicLink.issue(user=self.user, redirect_url="/")
        b = MagicLink.issue(user=self.user, redirect_url="/")
        self.assertNotEqual(a.token, b.token)

    def test_consume_marks_used_and_invalidates(self):
        link = MagicLink.issue(user=self.user, redirect_url="/")
        link.consume(ip="1.2.3.4")
        link.refresh_from_db()
        self.assertTrue(link.is_consumed)
        self.assertFalse(link.is_valid)
        self.assertEqual(link.used_ip, "1.2.3.4")

    def test_expired_link_is_not_valid(self):
        link = MagicLink.issue(user=self.user, redirect_url="/")
        link.expires_at = timezone.now() - timedelta(seconds=1)
        link.save(update_fields=["expires_at"])
        self.assertTrue(link.is_expired)
        self.assertFalse(link.is_valid)


class ConsumeMagicLinkViewTests(TestCase):
    """The portal magic-link endpoint is the entire customer auth surface — any
    bug here lets the wrong customer in, or lets a stolen link be reused."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="m@example.com", password="x")

    def _consume_url(self, token):
        return reverse("consume_magic_link", args=[token])

    def test_valid_link_logs_user_in_and_redirects(self):
        link = MagicLink.issue(user=self.user, redirect_url="/dashboard-target/")
        response = self.client.get(self._consume_url(link.token))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/dashboard-target/")
        # Session is now authenticated as the link's user.
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.id)
        link.refresh_from_db()
        self.assertTrue(link.is_consumed)

    def test_consumed_link_is_rejected_with_410(self):
        link = MagicLink.issue(user=self.user, redirect_url="/")
        link.consume()
        response = self.client.get(self._consume_url(link.token))
        self.assertEqual(response.status_code, 410)
        # Did NOT log anyone in.
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_expired_link_is_rejected_with_410(self):
        link = MagicLink.issue(user=self.user, redirect_url="/")
        link.expires_at = timezone.now() - timedelta(seconds=1)
        link.save(update_fields=["expires_at"])
        response = self.client.get(self._consume_url(link.token))
        self.assertEqual(response.status_code, 410)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_unknown_token_returns_404(self):
        response = self.client.get(self._consume_url("not-a-real-token"))
        self.assertEqual(response.status_code, 404)

    def test_consuming_twice_only_works_once(self):
        link = MagicLink.issue(user=self.user, redirect_url="/")
        first = self.client.get(self._consume_url(link.token))
        self.assertEqual(first.status_code, 302)
        # Fresh client (forget the session) and try again.
        self.client.logout()
        second = self.client.get(self._consume_url(link.token))
        self.assertEqual(second.status_code, 410)
