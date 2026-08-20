"""Connect / disconnect lifecycle: disconnect must revoke the grant at Xero
(not just forget our tokens — the org would pile up in Xero's connected apps),
and the callback must pick the org that was just authorised when the grant
carries more than one."""

from unittest import mock

from django.test import TestCase
from django.urls import reverse

from crm.models import XeroConnection

from .factories import make_associate, make_xero_connection


class XeroDisconnectTests(TestCase):
    def setUp(self):
        self.client.force_login(make_associate(is_finance=True))

    def test_revokes_at_xero_then_deletes_locally(self):
        conn = make_xero_connection(refresh_token="the-refresh-token")
        ok = mock.Mock(status_code=200)
        ok.raise_for_status.return_value = None
        with mock.patch("crm.xero.requests.post", return_value=ok) as post:
            response = self.client.post(reverse("crm:xero_disconnect"))
        self.assertRedirects(response, reverse("crm:xero_status"))
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args[0], "https://identity.xero.com/connect/revocation")
        self.assertEqual(post.call_args.kwargs["data"], {"token": "the-refresh-token"})
        self.assertFalse(XeroConnection.objects.exists())
        del conn

    def test_still_clears_locally_when_revocation_fails(self):
        make_xero_connection()
        with mock.patch("crm.xero.requests.post", side_effect=Exception("down")):
            response = self.client.post(reverse("crm:xero_disconnect"), follow=True)
        self.assertFalse(XeroConnection.objects.exists())
        self.assertContains(response, "confirm the revocation")
        self.assertContains(response, "Xero disconnected.")

    def test_no_revocation_call_without_connection(self):
        with mock.patch("crm.xero.requests.post") as post:
            self.client.post(reverse("crm:xero_disconnect"))
        post.assert_not_called()


class XeroCallbackTenantChoiceTests(TestCase):
    def setUp(self):
        self.client.force_login(make_associate(is_finance=True))
        session = self.client.session
        session["xero_oauth_state"] = "state-123"
        session.save()

    def _callback(self):
        return self.client.get(
            reverse("crm:xero_callback"), {"code": "auth-code", "state": "state-123"},
            follow=True,
        )

    def test_picks_most_recently_authorised_tenant(self):
        tokens = {"access_token": "at", "refresh_token": "rt", "expires_in": 1800}
        tenants = [
            {"tenantId": "old", "tenantName": "Old Org", "createdDateUtc": "2026-01-01T00:00:00"},
            {"tenantId": "new", "tenantName": "Demo Company (UK)", "createdDateUtc": "2026-08-20T12:00:00"},
        ]
        with (
            mock.patch("crm.xero.exchange_code_for_tokens", return_value=tokens),
            mock.patch("crm.xero.list_authorised_tenants", return_value=tenants),
        ):
            response = self._callback()
        conn = XeroConnection.objects.get()
        self.assertEqual(conn.tenant_id, "new")
        self.assertEqual(conn.tenant_name, "Demo Company (UK)")
        self.assertContains(response, "2 organisations authorised")
