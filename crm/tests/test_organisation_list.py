"""Organisation list: sortable columns and the owner/sector filters — and
that they compose with search."""

from django.test import TestCase
from django.urls import reverse

from crm.models import Organisation

from .factories import make_associate, make_organisation


def _names(response):
    return [o.name for o in response.context["object_list"]]


class OrganisationListTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = make_associate()

    def setUp(self):
        self.client.force_login(self.staff)

    def get(self, **params):
        return self.client.get(reverse("crm:organisation_list"), params)


class SortTests(OrganisationListTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.acme = make_organisation(name="Acme")
        cls.zenith = make_organisation(name="Zenith")

    def test_default_sorts_by_name(self):
        self.assertEqual(_names(self.get()), ["Acme", "Zenith"])

    def test_sort_by_name_both_directions(self):
        self.assertEqual(_names(self.get(sort="name")), ["Acme", "Zenith"])
        self.assertEqual(_names(self.get(sort="-name")), ["Zenith", "Acme"])

    def test_sort_by_owner_both_directions(self):
        # Unowned coalesces to "" (as on the deal list): first asc, last desc.
        self.zenith.owner = make_associate(first_name="Anna")
        self.zenith.save()
        self.assertEqual(_names(self.get(sort="owner")), ["Acme", "Zenith"])
        self.assertEqual(_names(self.get(sort="-owner")), ["Zenith", "Acme"])

    def test_sort_by_created(self):
        self.assertEqual(_names(self.get(sort="created")), ["Acme", "Zenith"])
        self.assertEqual(_names(self.get(sort="-created")), ["Zenith", "Acme"])

    def test_unknown_sort_falls_back_to_default(self):
        response = self.get(sort="bogus")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(_names(response), ["Acme", "Zenith"])

    def test_sort_by_sector_puts_unset_last(self):
        self.zenith.sector = Organisation.Sector.DENTAL
        self.zenith.save()
        self.assertEqual(_names(self.get(sort="sector")), ["Zenith", "Acme"])
        self.assertEqual(_names(self.get(sort="-sector")), ["Zenith", "Acme"])


class FilterTests(OrganisationListTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.other_staff = make_associate()
        cls.mine = make_organisation(name="Mine Ltd", owner=cls.staff)
        cls.theirs = make_organisation(name="Theirs Ltd", owner=cls.other_staff)
        cls.unowned = make_organisation(name="Unowned Ltd")

    def test_owner_filter(self):
        self.assertEqual(_names(self.get(owner=self.other_staff.pk)), ["Theirs Ltd"])
        self.assertEqual(_names(self.get(owner="me")), ["Mine Ltd"])

    def test_unowned_filter(self):
        self.assertEqual(_names(self.get(owner="none")), ["Unowned Ltd"])

    def test_search_and_filter_compose(self):
        response = self.get(q="Ltd", owner="me")
        self.assertEqual(_names(response), ["Mine Ltd"])

    def test_owner_column_rendered(self):
        make_organisation(name="Named Ltd", owner=make_associate(first_name="Nora", last_name="Field"))
        self.assertContains(self.get(), "Nora Field")


class SectorFilterTests(OrganisationListTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.dental = make_organisation(name="Dental Ltd", sector=Organisation.Sector.DENTAL)
        cls.vets = make_organisation(name="Vets Ltd", sector=Organisation.Sector.VETERINARY)
        cls.null_sector = make_organisation(name="Null Ltd")
        cls.blank_sector = make_organisation(name="Blank Ltd", sector="")

    def test_sector_filter(self):
        self.assertEqual(_names(self.get(sector="dental")), ["Dental Ltd"])
        self.assertEqual(_names(self.get(sector="veterinary")), ["Vets Ltd"])

    def test_none_matches_null_and_blank(self):
        self.assertEqual(_names(self.get(sector="none")), ["Blank Ltd", "Null Ltd"])

    def test_unknown_sector_ignored(self):
        self.assertEqual(len(_names(self.get(sector="garbage"))), 4)

    def test_composes_with_search_and_owner(self):
        self.dental.owner = self.staff
        self.dental.save()
        make_organisation(name="Dental Too", sector=Organisation.Sector.DENTAL)
        response = self.get(q="Dental", sector="dental", owner="me")
        self.assertEqual(_names(response), ["Dental Ltd"])
        self.assertContains(response, "sector=dental")

    def test_sector_column_shows_display_label(self):
        make_organisation(name="Physio Ltd", sector=Organisation.Sector.PHYSIO_CHIRO)
        self.assertContains(self.get(), "Physio/Chiro")
