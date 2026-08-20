"""Manual invoice-status sync — the nav button that pulls every mirrored
invoice's current status back from Xero. Network is mocked at the `requests`
layer, same as the DocuSeal wrapper tests."""

from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from .factories import (
    make_associate,
    make_deal,
    make_xero_connection,
    make_xero_invoice,
)


def _xero_response(invoices, status_code=200):
    r = mock.Mock()
    r.status_code = status_code
    r.json.return_value = {"Invoices": invoices}
    return r


class ListInvoicesWrapperTests(TestCase):
    def test_chunks_requests_and_concatenates(self):
        from crm import xero

        make_xero_connection()
        ids = [f"id-{i}" for i in range(xero._SYNC_CHUNK + 1)]
        with mock.patch(
            "crm.xero.requests.get",
            side_effect=[
                _xero_response([{"InvoiceID": "a"}]),
                _xero_response([{"InvoiceID": "b"}]),
            ],
        ) as get:
            result = xero.list_invoices(ids)
        self.assertEqual(get.call_count, 2)
        # First call carries a full chunk, second the remainder.
        first_ids = get.call_args_list[0].kwargs["params"]["IDs"]
        second_ids = get.call_args_list[1].kwargs["params"]["IDs"]
        self.assertEqual(len(first_ids.split(",")), xero._SYNC_CHUNK)
        self.assertEqual(second_ids, ids[-1])
        self.assertEqual([i["InvoiceID"] for i in result], ["a", "b"])

    def test_raises_without_connection(self):
        from crm import xero

        with self.assertRaises(xero.XeroError):
            xero.list_invoices(["id-1"])


class XeroSyncViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.finance = make_associate(is_finance=True)
        cls.deal = make_deal()

    def setUp(self):
        self.client.force_login(self.finance)

    def test_without_connection_redirects_to_status(self):
        response = self.client.post(reverse("crm:xero_sync"))
        self.assertRedirects(response, reverse("crm:xero_status"))

    def test_updates_changed_invoices(self):
        make_xero_connection()
        inv_paid = make_xero_invoice(self.deal, status="AUTHORISED")
        inv_same = make_xero_invoice(self.deal, status="AUTHORISED")
        remote = [
            {
                "InvoiceID": inv_paid.xero_invoice_id,
                "InvoiceNumber": inv_paid.xero_invoice_number,
                "Status": "PAID",
                "Total": 1200.00,
            },
            {
                "InvoiceID": inv_same.xero_invoice_id,
                "InvoiceNumber": inv_same.xero_invoice_number,
                "Status": "AUTHORISED",
            },
        ]
        with mock.patch("crm.xero.requests.get", return_value=_xero_response(remote)):
            response = self.client.post(reverse("crm:xero_sync"), follow=True)

        inv_paid.refresh_from_db()
        inv_same.refresh_from_db()
        self.assertEqual(inv_paid.status, "PAID")
        self.assertEqual(inv_paid.total, Decimal("1200.00"))
        self.assertEqual(inv_same.status, "AUTHORISED")
        self.assertContains(response, "Synced 2 Xero invoices — 1 updated.")

    def test_redirects_back_to_referer(self):
        make_xero_connection()
        deal_url = reverse("crm:deal_detail", args=[self.deal.pk])
        with mock.patch("crm.xero.requests.get", return_value=_xero_response([])):
            response = self.client.post(
                reverse("crm:xero_sync"),
                HTTP_REFERER=f"http://testserver{deal_url}",
            )
        self.assertRedirects(response, f"http://testserver{deal_url}")

    def test_xero_error_is_surfaced_not_raised(self):
        make_xero_connection()
        make_xero_invoice(self.deal)
        error = mock.Mock(status_code=500)
        error.json.return_value = {"Message": "boom"}
        error.text = "boom"
        with mock.patch("crm.xero.requests.get", return_value=error):
            response = self.client.post(reverse("crm:xero_sync"), follow=True)
        self.assertContains(response, "Xero said 500")


class SyncButtonVisibilityTests(TestCase):
    """The refresh button renders in the nav only for finance users AND only
    once a connection exists."""

    def _nav_html(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        return response

    def test_hidden_without_connection(self):
        self.client.force_login(make_associate(is_finance=True))
        self.assertNotContains(self._nav_html(), reverse("crm:xero_sync"))

    def test_shown_for_finance_user_when_connected(self):
        make_xero_connection()
        self.client.force_login(make_associate(is_finance=True))
        self.assertContains(self._nav_html(), reverse("crm:xero_sync"))

    def test_hidden_from_non_finance_staff_even_when_connected(self):
        make_xero_connection()
        self.client.force_login(make_associate())
        self.assertNotContains(self._nav_html(), reverse("crm:xero_sync"))
