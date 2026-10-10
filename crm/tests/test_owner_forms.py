"""Staff ownership of contacts and organisations: create/edit forms, detail
display, and the PROTECT constraint on the owning user. Also the small
choice fields that ride the same forms (organisation sector, deal type)."""

from django.db.models import ProtectedError
from django.test import TestCase
from django.urls import reverse

from crm.models import Contact, Deal, Organisation

from .factories import make_admin, make_associate, make_contact, make_deal, make_organisation


class OwnerFormTestCase(TestCase):
    """Owner-picking is an admin privilege — associates have the field
    removed (see test_associate_scoping)."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = make_admin()
        cls.colleague = make_associate()

    def setUp(self):
        self.client.force_login(self.staff)


class ContactOwnerTests(OwnerFormTestCase):
    def test_create_form_defaults_owner_to_current_user(self):
        response = self.client.get(reverse("crm:contact_create"))
        self.assertEqual(response.context["form"]["owner"].value(), self.staff.pk)
        self.assertEqual(response.context["owner_selected_id"], self.staff.pk)

    def test_create_persists_owner(self):
        response = self.client.post(reverse("crm:contact_create"), {
            "first_name": "Jane",
            "last_name": "Doe",
            "owner": self.colleague.pk,
        })
        contact = Contact.objects.get(last_name="Doe")
        self.assertRedirects(response, contact.get_absolute_url())
        self.assertEqual(contact.owner, self.colleague)

    def test_edit_can_clear_owner(self):
        contact = make_contact(owner=self.staff)
        response = self.client.post(
            reverse("crm:contact_update", args=[contact.pk]),
            {"first_name": contact.first_name, "last_name": contact.last_name},
        )
        contact.refresh_from_db()
        self.assertRedirects(response, contact.get_absolute_url())
        self.assertIsNone(contact.owner)

    def test_detail_shows_owner(self):
        owner = make_associate(first_name="Nora", last_name="Field")
        contact = make_contact(owner=owner)
        self.assertContains(self.client.get(contact.get_absolute_url()), "Nora Field")


class OrganisationOwnerTests(OwnerFormTestCase):
    def test_create_form_defaults_owner_to_current_user(self):
        response = self.client.get(reverse("crm:organisation_create"))
        self.assertEqual(response.context["form"]["owner"].value(), self.staff.pk)

    def test_create_persists_owner(self):
        response = self.client.post(reverse("crm:organisation_create"), {
            "name": "Owned Ltd",
            "owner": self.colleague.pk,
        })
        org = Organisation.objects.get(name="Owned Ltd")
        self.assertRedirects(response, org.get_absolute_url())
        self.assertEqual(org.owner, self.colleague)

    def test_edit_can_clear_owner(self):
        org = make_organisation(owner=self.staff)
        response = self.client.post(
            reverse("crm:organisation_update", args=[org.pk]), {"name": org.name},
        )
        org.refresh_from_db()
        self.assertRedirects(response, org.get_absolute_url())
        self.assertIsNone(org.owner)

    def test_detail_shows_owner(self):
        owner = make_associate(first_name="Nora", last_name="Field")
        org = make_organisation(owner=owner)
        self.assertContains(self.client.get(org.get_absolute_url()), "Nora Field")


class OrganisationSectorTests(OwnerFormTestCase):
    def test_create_persists_sector(self):
        response = self.client.post(reverse("crm:organisation_create"), {
            "name": "Toothy Ltd",
            "sector": Organisation.Sector.DENTAL,
        })
        org = Organisation.objects.get(name="Toothy Ltd")
        self.assertRedirects(response, org.get_absolute_url())
        self.assertEqual(org.sector, Organisation.Sector.DENTAL)

    def test_edit_clearing_sector_stores_null(self):
        org = make_organisation(sector=Organisation.Sector.DENTAL)
        response = self.client.post(
            reverse("crm:organisation_update", args=[org.pk]),
            {"name": org.name, "sector": ""},
        )
        org.refresh_from_db()
        self.assertRedirects(response, org.get_absolute_url())
        self.assertIsNone(org.sector)

    def test_detail_shows_sector_label(self):
        org = make_organisation(sector=Organisation.Sector.PHYSIO_CHIRO)
        self.assertContains(self.client.get(org.get_absolute_url()), "Physio/Chiro")

    def test_detail_omits_sector_line_when_unset(self):
        org = make_organisation()
        self.assertNotContains(self.client.get(org.get_absolute_url()), ">Sector<")


class DealTypeTests(OwnerFormTestCase):
    def test_create_persists_type(self):
        response = self.client.post(reverse("crm:deal_create"), {
            "name": "CT Scanner",
            "type": Deal.Type.ASSET_FINANCE,
            "owner": self.staff.pk,
        })
        deal = Deal.objects.get(name="CT Scanner")
        self.assertRedirects(response, deal.get_absolute_url())
        self.assertEqual(deal.type, Deal.Type.ASSET_FINANCE)

    def test_edit_clearing_type_stores_null(self):
        deal = make_deal(owner=self.staff, type=Deal.Type.COMMERCIAL_FINANCE)
        response = self.client.post(
            reverse("crm:deal_update", args=[deal.pk]),
            {"name": deal.name, "owner": self.staff.pk, "type": ""},
        )
        deal.refresh_from_db()
        self.assertRedirects(response, deal.get_absolute_url())
        self.assertIsNone(deal.type)

    def test_detail_shows_type_label(self):
        deal = make_deal(owner=self.staff, type=Deal.Type.COMMERCIAL_FINANCE)
        self.assertContains(self.client.get(deal.get_absolute_url()), "Commercial Finance")

    @staticmethod
    def _type_codes(response):
        """Flatten the grouped (optgroup) type choices to a list of codes."""
        codes = []
        for value, label in response.context["form"].fields["type"].choices:
            if isinstance(label, (list, tuple)):
                codes.extend(code for code, _ in label)
            else:
                codes.append(value)
        return codes

    def test_create_form_groups_types_under_headings(self):
        response = self.client.get(reverse("crm:deal_create"))
        choices = response.context["form"].fields["type"].choices
        groups = {label: [c for c, _ in opts] for label, opts in choices if isinstance(opts, list)}
        self.assertEqual(groups["Asset Finance"], [Deal.Type.ASSET_FINANCE])
        self.assertIn(Deal.Type.TAX_LOAN, groups["Commercial Finance"])
        self.assertNotIn(Deal.Type.ASSET_FINANCE, groups["Commercial Finance"])
        self.assertContains(response, '<optgroup label="Asset Finance">')
        self.assertContains(response, '<optgroup label="Commercial Finance">')

    def test_create_form_offers_new_types_and_hides_retired_ones(self):
        response = self.client.get(reverse("crm:deal_create"))
        codes = self._type_codes(response)
        self.assertIn(Deal.Type.TAX_LOAN, codes)
        self.assertIn(Deal.Type.PARTNER_BUY_IN, codes)
        self.assertIn(Deal.Type.ASSET_FINANCE, codes)
        for retired in Deal.RETIRED_TYPES:
            self.assertNotIn(retired, codes)

    def test_edit_form_keeps_only_the_deals_own_retired_type(self):
        deal = make_deal(owner=self.staff, type=Deal.Type.KMC_ASSOCIATES)
        response = self.client.get(reverse("crm:deal_update", args=[deal.pk]))
        codes = self._type_codes(response)
        self.assertIn(Deal.Type.KMC_ASSOCIATES, codes)
        self.assertNotIn(Deal.Type.STRUCTURED_FINANCE, codes)
        # Re-saving the edit form with the retired type untouched still validates.
        response = self.client.post(
            reverse("crm:deal_update", args=[deal.pk]),
            {"name": deal.name, "owner": self.staff.pk, "type": Deal.Type.KMC_ASSOCIATES},
        )
        deal.refresh_from_db()
        self.assertRedirects(response, deal.get_absolute_url())
        self.assertEqual(deal.type, Deal.Type.KMC_ASSOCIATES)

    def test_create_rejects_retired_type(self):
        response = self.client.post(reverse("crm:deal_create"), {
            "name": "Old style",
            "type": Deal.Type.STRUCTURED_FINANCE,
            "owner": self.staff.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertFormError(response.context["form"], "type",
                             "Select a valid choice. structured_finance is not one of the available choices.")
        self.assertFalse(Deal.objects.filter(name="Old style").exists())

    def test_detail_omits_type_line_when_unset(self):
        deal = make_deal(owner=self.staff)
        self.assertNotContains(self.client.get(deal.get_absolute_url()), "Type:")


class DealSourceTests(OwnerFormTestCase):
    def test_create_persists_source(self):
        response = self.client.post(reverse("crm:deal_create"), {
            "name": "Dental chair",
            "source": Deal.Source.SUPPLIER,
            "owner": self.staff.pk,
        })
        deal = Deal.objects.get(name="Dental chair")
        self.assertRedirects(response, deal.get_absolute_url())
        self.assertEqual(deal.source, Deal.Source.SUPPLIER)

    def test_edit_clearing_source_stores_null(self):
        deal = make_deal(owner=self.staff, source=Deal.Source.REFERRAL)
        response = self.client.post(
            reverse("crm:deal_update", args=[deal.pk]),
            {"name": deal.name, "owner": self.staff.pk, "source": ""},
        )
        deal.refresh_from_db()
        self.assertRedirects(response, deal.get_absolute_url())
        self.assertIsNone(deal.source)

    def test_detail_shows_source_label(self):
        deal = make_deal(owner=self.staff, source=Deal.Source.EXISTING_CUSTOMER)
        self.assertContains(self.client.get(deal.get_absolute_url()), "Source: Existing Customer")

    def test_detail_omits_source_line_when_unset(self):
        deal = make_deal(owner=self.staff)
        self.assertNotContains(self.client.get(deal.get_absolute_url()), "Source:")


class OwnerProtectTests(OwnerFormTestCase):
    def test_deleting_user_who_owns_contact_is_protected(self):
        make_contact(owner=self.colleague)
        with self.assertRaises(ProtectedError):
            self.colleague.delete()

    def test_deleting_user_who_owns_organisation_is_protected(self):
        make_organisation(owner=self.colleague)
        with self.assertRaises(ProtectedError):
            self.colleague.delete()
