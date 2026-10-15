"""Associates linked to an organisation share deals with their colleagues.

`User.organisation` groups associates into a team: everyone in the same
organisation sees — and can act on — every deal owned by anyone in it,
exactly as if it were their own. Deals owned by staff in other organisations,
or by nobody, stay out of reach. The owner filter and owner field open up
for them, limited to the organisation's staff. An associate with no
organisation keeps the own-deals-only behaviour (test_associate_scoping).
"""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from crm.access import owner_choices, visible_deals
from crm.models import Contact, Deal, Note, Organisation

from .factories import (
    make_admin,
    make_associate,
    make_deal,
    make_document,
    make_organisation,
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
        # none rather than silently defaulting to the submitter.
        response = self.client.post(reverse("crm:organisation_create"), {"name": "No owner given"})
        org = Organisation.objects.get(name="No owner given")
        self.assertRedirects(response, org.get_absolute_url())
        self.assertIsNone(org.owner)

    def test_edit_keeps_owner_who_left_the_organisation(self):
        contact = Contact.objects.create(first_name="Old", last_name="Timer", owner=self.colleague)
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
