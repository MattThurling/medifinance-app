"""crm.portal_checklist — what the deal page says we still need."""

from django.test import TestCase

from crm.models import Document, SignatureRequest
from crm.portal_checklist import (
    contact_missing_fields,
    organisation_missing_fields,
    outstanding_items,
)

from .factories import (
    make_contact,
    make_customer_with_deal,
    make_document,
    make_quote,
    make_signature_request,
)
from .test_customer_portal import complete_details


class MissingFieldTests(TestCase):
    def test_contact_fields(self):
        c = make_contact()
        self.assertEqual(contact_missing_fields(c), ["date of birth", "phone", "home address"])
        c.date_of_birth = "1980-01-01"
        c.phone = "0123"
        c.home_address_line1 = "1 St"
        self.assertEqual(contact_missing_fields(c), ["home address"])  # postcode still blank
        c.home_address_postcode = "AB1 2CD"
        self.assertEqual(contact_missing_fields(c), [])

    def test_organisation_fields(self):
        _, deal = make_customer_with_deal()
        org = deal.organisation
        self.assertEqual(organisation_missing_fields(org), ["address", "phone"])
        org.address_line1 = "2 Rd"
        org.address_postcode = "AB1 2CD"
        org.phone = "0123"
        self.assertEqual(organisation_missing_fields(org), [])


class OutstandingItemsTests(TestCase):
    def setUp(self):
        self.user, self.deal = make_customer_with_deal()
        complete_details(self.deal)

    def labels(self):
        return [i.label for i in outstanding_items(self.deal, user=self.user)]

    def test_empty_when_complete(self):
        self.assertEqual(self.labels(), [])

    def test_own_details_say_your_details_and_name_co_applicants(self):
        me = self.deal.customer
        me.phone = ""
        me.save()
        co = make_contact(first_name="Jane", last_name="Co")
        self.deal.co_applicants.add(co)
        items = outstanding_items(self.deal, user=self.user)
        self.assertEqual([i.label for i in items],
                         ["Your details: phone", "Jane Co: date of birth, phone, home address"])
        self.assertEqual([i.anchor for i in items], [f"person-{me.pk}", f"person-{co.pk}"])

    def test_company_item(self):
        org = self.deal.organisation
        org.phone = ""
        org.save()
        items = outstanding_items(self.deal)
        self.assertEqual([(i.label, i.anchor) for i in items], [("Company details: phone", "company")])

    def test_quote_only_when_quotes_exist_and_none_chosen(self):
        self.assertNotIn("Choose your quote", self.labels())
        quote = make_quote(self.deal)
        self.assertIn("Choose your quote", self.labels())
        self.deal.selected_quote = quote
        self.deal.save(update_fields=["selected_quote"])
        self.assertNotIn("Choose your quote", self.labels())

    def test_documents_and_signatures_in_page_order(self):
        make_document(self.deal, name="Bank statements")
        provided = make_document(self.deal, name="ID")
        provided.status = Document.Status.PROVIDED
        provided.save(update_fields=["status"])
        make_signature_request(self.deal, template_name="HP agreement")
        make_signature_request(self.deal, status=SignatureRequest.Status.COMPLETED)
        make_quote(self.deal)
        self.assertEqual(self.labels(),
                         ["Choose your quote", "Upload: Bank statements", "Sign: HP agreement"])
