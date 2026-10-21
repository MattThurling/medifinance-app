"""Associates linked to an organisation share deals with their colleagues.

`User.organisation` groups associates into a team: everyone in the same
organisation sees — and can act on — every deal, contact and organisation
owned by anyone in it, exactly as if it were their own. Deals owned by staff
in other organisations, or by nobody, stay out of reach; contacts and
organisations are softer — unowned ones are visible to everyone, and only
those owned by staff outside the team are hidden. The owner filter and owner
field open up for them, limited to the organisation's staff. An associate
with no organisation keeps the own-records-only behaviour
(test_associate_scoping).
"""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from crm.access import owner_choices, visible_contacts, visible_deals, visible_organisations
from crm.models import Contact, Deal, Note, Organisation

from .factories import (
    make_admin,
    make_associate,
    make_contact,
    make_deal,
    make_document,
    make_organisation,
    make_participation,
    make_quote,
)


class OrgScopingTestCase(TestCase):
    """`associate` and `colleague` share `org`; `outsider` is in another
    organisation; `loner` has none."""

    @classmethod
    def setUpTestData(cls):
        cls.org = make_organisation(name="Team Org")
        cls.other_org = make_organisation(name="Other Org")
        cls.admin = make_admin()
        cls.associate = make_associate(organisation=cls.org, first_name="Ann", last_name="Associate")
        cls.colleague = make_associate(organisation=cls.org, first_name="Col", last_name="League")
        cls.outsider = make_associate(organisation=cls.other_org, first_name="Out", last_name="Sider")
        cls.loner = make_associate(first_name="Lone", last_name="Wolf")
        cls.own = make_deal(owner=cls.associate, name="Own deal")
        cls.colleagues = make_deal(owner=cls.colleague, name="Colleague deal")
        cls.outsiders = make_deal(owner=cls.outsider, name="Outsider deal")
        cls.loners = make_deal(owner=cls.loner, name="Loner deal")
        cls.unowned = make_deal(name="Unowned deal")
        Deal.objects.filter(pk=cls.unowned.pk).update(owner=None)
        cls.unowned.refresh_from_db()

    def setUp(self):
        self.client.force_login(self.associate)


class HelperTests(OrgScopingTestCase):
    def test_visible_deals_by_role(self):
        self.assertCountEqual(visible_deals(self.associate), [self.own, self.colleagues])
        self.assertCountEqual(visible_deals(self.colleague), [self.own, self.colleagues])
        self.assertCountEqual(visible_deals(self.outsider), [self.outsiders])
        self.assertCountEqual(visible_deals(self.loner), [self.loners])
        self.assertCountEqual(
            visible_deals(self.admin),
            [self.own, self.colleagues, self.outsiders, self.loners, self.unowned],
        )

    def test_admin_in_an_organisation_still_sees_everything(self):
        admin = make_admin(organisation=self.org)
        self.assertEqual(visible_deals(admin).count(), 5)
        # ... and their deals are shared with the org's associates.
        theirs = make_deal(owner=admin, name="Admin's deal")
        self.assertIn(theirs, visible_deals(self.associate))

    def test_owner_choices(self):
        self.assertCountEqual(owner_choices(self.associate), [self.associate, self.colleague])
        self.assertCountEqual(owner_choices(self.loner), [self.loner])
        staff = list(owner_choices(self.admin))
        for u in (self.admin, self.associate, self.colleague, self.outsider, self.loner):
            self.assertIn(u, staff)
        from django.contrib.auth.models import AnonymousUser
        self.assertEqual(list(owner_choices(AnonymousUser())), [])


class DealListTests(OrgScopingTestCase):
    def _names(self, **params):
        response = self.client.get(reverse("crm:deal_list"), params)
        self.assertEqual(response.status_code, 200)
        return response, [d.name for d in response.context["object_list"]]

    def test_list_shows_organisations_deals(self):
        response, names = self._names()
        self.assertCountEqual(names, ["Own deal", "Colleague deal"])
        self.assertNotContains(response, "Out Sider")
        self.assertNotContains(response, "Lone Wolf")

    def test_owner_filter_unlocked_and_limited_to_colleagues(self):
        response, _ = self._names()
        self.assertFalse(response.context["owner_filter_locked"])
        self.assertCountEqual(response.context["owner_choices"], [self.associate, self.colleague])
        self.assertContains(response, 'name="owner"')
        self.assertContains(response, "Col League")
        self.assertNotContains(response, "Unowned</option>")

    def test_owner_filter_applies_within_the_organisation(self):
        _, names = self._names(owner=str(self.colleague.pk))
        self.assertEqual(names, ["Colleague deal"])
        _, names = self._names(owner="me")
        self.assertEqual(names, ["Own deal"])
        _, names = self._names(owner=str(self.outsider.pk))
        self.assertEqual(names, [])
        _, names = self._names(owner="none")
        self.assertEqual(names, [])

    def test_maturing_list_unlocked(self):
        response = self.client.get(reverse("crm:deal_maturing"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["owner_filter_locked"])
        self.assertContains(response, 'name="owner"')
        self.assertNotContains(response, "Unowned</option>")

    def test_loner_still_locked(self):
        self.client.force_login(self.loner)
        response, names = self._names()
        self.assertEqual(names, ["Loner deal"])
        self.assertTrue(response.context["owner_filter_locked"])
        self.assertNotContains(response, 'name="owner"')


class DealPageTests(OrgScopingTestCase):
    def test_colleague_deal_pages_render(self):
        for name in ("deal_detail", "deal_overview", "deal_update"):
            with self.subTest(view=name):
                response = self.client.get(reverse(f"crm:{name}", args=[self.colleagues.pk]))
                self.assertEqual(response.status_code, 200)

    def test_other_organisations_and_unowned_deals_404(self):
        for name in ("deal_detail", "deal_overview", "deal_update", "deal_delete"):
            for deal in (self.outsiders, self.loners, self.unowned):
                with self.subTest(view=name, deal=deal.name):
                    response = self.client.get(reverse(f"crm:{name}", args=[deal.pk]))
                    self.assertEqual(response.status_code, 404)

    def test_can_edit_colleague_deal(self):
        response = self.client.post(
            reverse("crm:deal_update", args=[self.colleagues.pk]),
            {"name": "Renamed by colleague", "type": "", "source": "", "owner": self.colleague.pk},
        )
        self.assertRedirects(response, self.colleagues.get_absolute_url())
        self.colleagues.refresh_from_db()
        self.assertEqual(self.colleagues.name, "Renamed by colleague")
        self.assertEqual(self.colleagues.owner, self.colleague)

    def test_child_create_on_colleague_deal(self):
        for name in ("quote_create", "proposal_create", "participation_create",
                     "document_create", "stage_create"):
            with self.subTest(view=name):
                url = reverse(f"crm:{name}") + f"?deal={self.colleagues.pk}"
                self.assertEqual(self.client.get(url).status_code, 200)
                url = reverse(f"crm:{name}") + f"?deal={self.outsiders.pk}"
                self.assertEqual(self.client.get(url).status_code, 404)

    def test_note_on_colleague_deal(self):
        response = self.client.post(
            reverse("crm:note_create"), {"deal": self.colleagues.pk, "content": "team note"},
        )
        self.assertRedirects(response, self.colleagues.get_absolute_url())
        self.assertTrue(Note.objects.filter(deal=self.colleagues, content="team note").exists())
        response = self.client.post(
            reverse("crm:note_create"), {"deal": self.outsiders.pk, "content": "psst"},
        )
        self.assertEqual(response.status_code, 404)

    def test_children_of_colleague_deal_reachable(self):
        quote = make_quote(self.colleagues)
        response = self.client.get(reverse("crm:quote_update", args=[quote.pk]))
        self.assertEqual(response.status_code, 200)
        response = self.client.post(reverse("crm:quote_select", args=[quote.pk]))
        self.assertRedirects(response, self.colleagues.get_absolute_url())
        self.colleagues.refresh_from_db()
        self.assertEqual(self.colleagues.selected_quote_id, quote.pk)

        their_quote = make_quote(self.outsiders)
        self.assertEqual(
            self.client.get(reverse("crm:quote_update", args=[their_quote.pk])).status_code, 404,
        )

    def test_documents_on_colleague_deal(self):
        doc = make_document(self.colleagues)
        response = self.client.post(
            reverse("crm:document_upload", args=[doc.pk]),
            {"file": SimpleUploadedFile("c.pdf", b"%PDF-C")},
        )
        self.assertRedirects(response, self.colleagues.get_absolute_url())
        doc.refresh_from_db()
        self.assertTrue(doc.file)
        self.assertEqual(
            self.client.get(reverse("crm:document_download", args=[doc.pk])).status_code, 200,
        )

        their_doc = make_document(self.outsiders)
        their_doc.attach(SimpleUploadedFile("a.pdf", b"%PDF-A"), by=self.outsider)
        self.assertEqual(
            self.client.get(reverse("crm:document_download", args=[their_doc.pk])).status_code, 403,
        )


class OwnerFieldTests(OrgScopingTestCase):
    CREATE_VIEWS = ("deal_create", "contact_create", "organisation_create")

    def test_forms_offer_owner_limited_to_colleagues(self):
        for name in self.CREATE_VIEWS:
            with self.subTest(view=name):
                response = self.client.get(reverse(f"crm:{name}"))
                self.assertEqual(response.status_code, 200)
                form = response.context["form"]
                self.assertIn("owner", form.fields)
                self.assertFalse(response.context["owner_locked"])
                self.assertCountEqual(form.fields["owner"].queryset, [self.associate, self.colleague])
                self.assertEqual(form["owner"].value(), self.associate.pk)
                self.assertContains(response, 'name="owner"')

    def test_can_assign_colleague_but_not_outsider(self):
        response = self.client.post(reverse("crm:deal_create"), {
            "name": "Handed over", "type": "", "source": "", "owner": self.colleague.pk,
        })
        deal = Deal.objects.get(name="Handed over")
        self.assertRedirects(response, deal.get_absolute_url())
        self.assertEqual(deal.owner, self.colleague)

        response = self.client.post(reverse("crm:contact_create"), {
            "first_name": "Bad", "last_name": "Owner", "owner": self.outsider.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertFormError(response.context["form"], "owner", [
            "Select a valid choice. That choice is not one of the available choices."
        ])
        self.assertFalse(Contact.objects.filter(last_name="Owner").exists())

    def test_omitted_owner_stays_empty(self):
        # Organisation.owner is optional, so an omitted owner is saved as
        # none rather than silently defaulting to the submitter — and the
        # unowned org is still visible to its creator.
        response = self.client.post(reverse("crm:organisation_create"), {"name": "No owner given"})
        org = Organisation.objects.get(name="No owner given")
        self.assertRedirects(response, org.get_absolute_url())
        self.assertIsNone(org.owner)

    def test_edit_keeps_owner_who_left_the_organisation(self):
        contact = Contact.objects.create(first_name="Old", last_name="Timer", owner=self.colleague)
        # Keep the contact reachable once the colleague has left: it's the
        # customer on one of our deals.
        self.own.customer = contact
        self.own.save(update_fields=["customer"])
        self.colleague.organisation = None
        self.colleague.save(update_fields=["organisation"])
        response = self.client.get(reverse("crm:contact_update", args=[contact.pk]))
        self.assertIn(self.colleague, response.context["form"].fields["owner"].queryset)
        response = self.client.post(
            reverse("crm:contact_update", args=[contact.pk]),
            {"first_name": "Still", "last_name": "Timer", "owner": self.colleague.pk},
        )
        self.assertRedirects(response, contact.get_absolute_url())
        contact.refresh_from_db()
        self.assertEqual(contact.owner, self.colleague)

    def test_loner_still_has_no_owner_field(self):
        self.client.force_login(self.loner)
        response = self.client.get(reverse("crm:deal_create"))
        self.assertNotIn("owner", response.context["form"].fields)
        self.assertTrue(response.context["owner_locked"])


class AddressBookTeamTests(OrgScopingTestCase):
    """Contacts and organisations are shared across the team the same way
    deals are. Unowned records (every deal's customer and organisation here,
    plus `loose_*`) are visible to everyone; records owned by staff outside
    the team are hidden unless attached to a visible deal."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.mine = make_contact(owner=cls.associate, last_name="Mine")
        cls.colleague_contact = make_contact(owner=cls.colleague, last_name="Colleague-owned")
        cls.outsider_contact = make_contact(owner=cls.outsider, last_name="Outsider-owned")
        cls.loner_contact = make_contact(owner=cls.loner, last_name="Loner-owned")
        cls.loose_contact = make_contact(last_name="Loose")
        cls.my_org = make_organisation(name="My Org", owner=cls.associate)
        cls.colleague_org = make_organisation(name="Colleague Org", owner=cls.colleague)
        cls.outsider_org = make_organisation(name="Outsider Org", owner=cls.outsider)
        cls.loose_org = make_organisation(name="Loose Org")
        # Outsider-owned supplier on a colleague's deal: reachable.
        cls.supplier = make_organisation(name="Supplier", owner=cls.outsider)
        make_participation(cls.colleagues, organisation=cls.supplier)
        cls.unowned_contacts = list(Contact.objects.filter(owner__isnull=True))
        cls.unowned_orgs = list(Organisation.objects.filter(owner__isnull=True))

    def test_visible_contacts_by_team(self):
        expected = self.unowned_contacts + [self.mine, self.colleague_contact]
        self.assertCountEqual(visible_contacts(self.associate), expected)
        self.assertCountEqual(visible_contacts(self.colleague), expected)
        self.assertCountEqual(
            visible_contacts(self.outsider), self.unowned_contacts + [self.outsider_contact],
        )
        self.assertCountEqual(
            visible_contacts(self.loner), self.unowned_contacts + [self.loner_contact],
        )
        self.assertCountEqual(visible_contacts(self.admin), Contact.objects.all())

    def test_visible_organisations_by_team(self):
        expected = self.unowned_orgs + [self.my_org, self.colleague_org, self.supplier]
        self.assertCountEqual(visible_organisations(self.associate), expected)
        self.assertCountEqual(visible_organisations(self.colleague), expected)
        self.assertCountEqual(
            visible_organisations(self.outsider),
            self.unowned_orgs + [self.outsider_org, self.supplier],
        )
        self.assertCountEqual(visible_organisations(self.loner), self.unowned_orgs)
        self.assertCountEqual(visible_organisations(self.admin), Organisation.objects.all())

    def test_lists_unlocked_and_limited_to_colleagues(self):
        response = self.client.get(reverse("crm:contact_list"))
        self.assertCountEqual(
            response.context["object_list"],
            self.unowned_contacts + [self.mine, self.colleague_contact],
        )
        self.assertFalse(response.context["owner_filter_locked"])
        self.assertCountEqual(response.context["owner_choices"], [self.associate, self.colleague])
        self.assertContains(response, 'name="owner"')
        self.assertContains(response, ">Unowned</option>")
        self.assertNotContains(response, "Out Sider")
        self.assertNotContains(response, "Outsider-owned")

        response = self.client.get(reverse("crm:contact_list"), {"owner": str(self.colleague.pk)})
        self.assertEqual(list(response.context["object_list"]), [self.colleague_contact])
        response = self.client.get(reverse("crm:contact_list"), {"owner": str(self.outsider.pk)})
        self.assertEqual(list(response.context["object_list"]), [])
        response = self.client.get(reverse("crm:contact_list"), {"owner": "me"})
        self.assertEqual(list(response.context["object_list"]), [self.mine])
        response = self.client.get(reverse("crm:contact_list"), {"owner": "none"})
        self.assertCountEqual(response.context["object_list"], self.unowned_contacts)

        response = self.client.get(reverse("crm:organisation_list"))
        self.assertCountEqual(
            response.context["object_list"],
            self.unowned_orgs + [self.my_org, self.colleague_org, self.supplier],
        )
        self.assertFalse(response.context["owner_filter_locked"])
        self.assertContains(response, ">Unowned</option>")
        self.assertNotContains(response, "Outsider Org")
        response = self.client.get(reverse("crm:organisation_list"), {"owner": str(self.colleague.pk)})
        self.assertEqual(list(response.context["object_list"]), [self.colleague_org])

    def test_colleague_and_unowned_pages_render_outsider_404(self):
        for name, obj in (
            ("contact_detail", self.colleague_contact),
            ("contact_update", self.colleague_contact),
            ("contact_detail", self.colleagues.customer),
            ("contact_update", self.loose_contact),
            ("contact_detail", self.unowned.customer),
            ("organisation_detail", self.colleague_org),
            ("organisation_update", self.colleague_org),
            ("organisation_detail", self.supplier),
            ("organisation_update", self.loose_org),
            ("organisation_detail", self.unowned.organisation),
        ):
            with self.subTest(view=name, obj=str(obj)):
                self.assertEqual(self.client.get(reverse(f"crm:{name}", args=[obj.pk])).status_code, 200)
        for name, obj in (
            ("contact_detail", self.outsider_contact),
            ("contact_update", self.loner_contact),
            ("contact_delete", self.outsider_contact),
            ("organisation_detail", self.outsider_org),
            ("organisation_update", self.outsider_org),
            ("organisation_delete", self.outsider_org),
        ):
            with self.subTest(view=name, obj=str(obj)):
                self.assertEqual(self.client.get(reverse(f"crm:{name}", args=[obj.pk])).status_code, 404)

    def test_can_edit_colleague_contact(self):
        response = self.client.post(
            reverse("crm:contact_update", args=[self.colleague_contact.pk]),
            {"first_name": "Edited", "last_name": "Colleague-owned", "owner": self.colleague.pk},
        )
        self.assertRedirects(response, self.colleague_contact.get_absolute_url())
        self.colleague_contact.refresh_from_db()
        self.assertEqual(self.colleague_contact.first_name, "Edited")

    def test_contact_search_finds_team_and_unowned_contacts(self):
        response = self.client.get(reverse("crm:contact_search"), {"q": "owned"})
        self.assertCountEqual(response.context["results"], [self.colleague_contact])
        response = self.client.get(reverse("crm:contact_search"), {"q": "Loose"})
        self.assertEqual(list(response.context["results"]), [self.loose_contact])

    def test_dashboard_counts_the_teams_records(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.context["contacts_count"], len(self.unowned_contacts) + 2)
        self.assertEqual(response.context["organisations_count"], len(self.unowned_orgs) + 3)
        self.client.force_login(self.loner)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.context["contacts_count"], len(self.unowned_contacts) + 1)
        self.assertEqual(response.context["organisations_count"], len(self.unowned_orgs))


class UserSearchTests(OrgScopingTestCase):
    def test_search_limited_to_colleagues(self):
        response = self.client.get(reverse("crm:user_search"), {"q": "e"})
        self.assertCountEqual(response.context["results"], [self.associate, self.colleague])

    def test_loner_only_finds_themselves(self):
        self.client.force_login(self.loner)
        response = self.client.get(reverse("crm:user_search"), {"q": "o"})
        self.assertEqual(list(response.context["results"]), [self.loner])

    def test_admin_finds_everyone(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("crm:user_search"), {"q": "Sider"})
        self.assertEqual(list(response.context["results"]), [self.outsider])


class ReportsAndDashboardTests(OrgScopingTestCase):
    def test_reports_cover_the_organisation(self):
        response = self.client.get(reverse("crm:reports"))
        self.assertEqual(response.status_code, 200)
        p = response.context["params"]
        self.assertEqual(p.owner, "")
        self.assertEqual(p.organisation_id, self.org.pk)
        self.assertFalse(response.context["owner_filter_locked"])
        self.assertEqual(response.context["headline"]["new_deals"]["value"], 2)
        self.assertContains(response, "By owner")
        self.assertEqual(
            {o["name"] for o in response.context["owners"]}, {"Ann Associate", "Col League"},
        )
        self.assertNotContains(response, "Out Sider")
        self.assertNotContains(response, "Lone Wolf")
        self.assertNotContains(response, "Unowned")

    def test_reports_owner_filter_within_the_organisation(self):
        response = self.client.get(reverse("crm:reports"), {"owner": str(self.colleague.pk)})
        self.assertEqual(response.context["params"].owner, str(self.colleague.pk))
        self.assertEqual(response.context["headline"]["new_deals"]["value"], 1)
        response = self.client.get(reverse("crm:reports"), {"owner": str(self.outsider.pk)})
        self.assertEqual(response.context["headline"]["new_deals"]["value"], 0)
        response = self.client.get(reverse("crm:reports"), {"owner": "none"})
        self.assertEqual(response.context["headline"]["new_deals"]["value"], 0)

    def test_dashboard_counts_the_organisations_deals(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.context["deals_count"], 2)
        self.assertEqual(response.context["stats"]["headline"]["new_deals"]["value"], 2)
        self.client.force_login(self.loner)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.context["deals_count"], 1)


class StaffApiTests(OrgScopingTestCase):
    def test_list_and_detail_cover_the_organisation(self):
        response = self.client.get(reverse("crm_api:deal_list"))
        self.assertCountEqual(
            [d["name"] for d in response.json()["deals"]], ["Own deal", "Colleague deal"],
        )
        self.assertEqual(
            self.client.get(reverse("crm_api:deal_detail", args=[self.colleagues.pk])).status_code, 200,
        )
        for deal in (self.outsiders, self.loners, self.unowned):
            self.assertEqual(
                self.client.get(reverse("crm_api:deal_detail", args=[deal.pk])).status_code, 404,
            )
