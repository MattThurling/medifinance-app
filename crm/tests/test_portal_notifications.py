"""Staff notification emails fired by the customer portal wizard."""

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from crm.models import Stage

from .factories import make_customer_with_deal, make_quote


@override_settings(NOTIFY_EMAILS=["staff@medi-finance.co.uk"])
class PortalApplicationSubmittedEmailTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer, cls.deal = make_customer_with_deal()
        cls.deal.customer.first_name = "Jane"
        cls.deal.customer.last_name = "Smith"
        cls.deal.customer.email = "jane@example.com"
        cls.deal.customer.save()
        cls.deal.organisation.name = "Smith Dental Ltd"
        cls.deal.organisation.save()
        cls.deal.selected_quote = make_quote(cls.deal)
        cls.deal.save(update_fields=["selected_quote"])

    def _management_form(self):
        return {
            "co-TOTAL_FORMS": "0",
            "co-INITIAL_FORMS": "0",
            "co-MIN_NUM_FORMS": "0",
            "co-MAX_NUM_FORMS": "1000",
        }

    def _post(self):
        self.client.force_login(self.customer)
        return self.client.post(
            reverse("crm:portal_applicants", args=[self.deal.pk]),
            {
                "customer-first_name": "Jane",
                "customer-last_name": "Smith",
                **self._management_form(),
            },
        )

    def test_first_submission_sends_notification(self):
        response = self._post()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["staff@medi-finance.co.uk"])
        self.assertIn(self.deal.name, message.subject)
        self.assertIn("Smith Dental Ltd", message.body)
        self.assertIn("jane@example.com", message.body)
        self.assertIn(self.deal.get_absolute_url(), message.body)

    def test_second_submission_does_not_resend(self):
        """The stage event is only created once, so the email mirrors that."""
        self._post()
        mail.outbox.clear()
        self._post()
        self.assertEqual(Stage.objects.filter(
            deal=self.deal, name=Stage.Name.INFO_RECEIVED,
        ).count(), 1)
        self.assertEqual(mail.outbox, [])
