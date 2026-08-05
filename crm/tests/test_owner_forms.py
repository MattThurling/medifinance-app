"""Staff ownership of contacts and organisations: create/edit forms, detail
display, and the PROTECT constraint on the owning user. Also the small
choice fields that ride the same forms (organisation sector, deal type)."""

from django.db.models import ProtectedError
from django.test import TestCase
from django.urls import reverse

from crm.models import Contact, Deal, Organisation

from .factories import make_associate, make_contact, make_deal, make_organisation


class OwnerFormTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = make_associate()
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

    def test_detail_omits_type_line_when_unset(self):
        deal = make_deal(owner=self.staff)
        self.assertNotContains(self.client.get(deal.get_absolute_url()), "Type:")


class OwnerProtectTests(OwnerFormTestCase):
    def test_deleting_user_who_owns_contact_is_protected(self):
        make_contact(owner=self.colleague)
        with self.assertRaises(ProtectedError):
            self.colleague.delete()

    def test_deleting_user_who_owns_organisation_is_protected(self):
        make_organisation(owner=self.colleague)
        with self.assertRaises(ProtectedError):
            self.colleague.delete()
