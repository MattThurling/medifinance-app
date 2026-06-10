from django.test import TestCase

from accounts.models import ApiKey, User
from crm.models import Organisation


def _org(name: str = "Acme Brokers") -> Organisation:
    return Organisation.objects.create(name=name)


class ApiKeyIssueTests(TestCase):
    def test_issue_returns_instance_and_raw_key(self):
        creator = User.objects.create_user(email="staff@example.com", password="x")
        org = _org()
        key, raw = ApiKey.issue(organisation=org, created_by=creator)

        self.assertIsNotNone(key.pk)
        self.assertEqual(key.organisation, org)
        self.assertEqual(key.created_by, creator)
        self.assertTrue(key.is_active)
        self.assertIsNone(key.last_used_at)
        # Raw key is recognisable — prefix lets us spot it in logs.
        self.assertTrue(raw.startswith("mfk_"))
        # Prefix on the model matches the first N chars of the raw key.
        self.assertTrue(raw.startswith(key.prefix))
        self.assertEqual(len(key.prefix), ApiKey.PREFIX_VISIBLE_LEN)

    def test_raw_key_is_never_stored(self):
        _, raw = ApiKey.issue(organisation=_org())
        # Hash is stored, not the raw key.
        self.assertNotIn(raw, ApiKey.objects.values_list("hashed_key", flat=True))

    def test_two_issued_keys_are_distinct(self):
        org = _org()
        _, a = ApiKey.issue(organisation=org)
        _, b = ApiKey.issue(organisation=org)
        self.assertNotEqual(a, b)

    def test_one_organisation_can_hold_multiple_keys(self):
        """During rotation an integrator briefly holds two keys — issue the
        new one, swap, revoke the old. The schema mustn't block that."""
        org = _org()
        first, _ = ApiKey.issue(organisation=org)
        second, _ = ApiKey.issue(organisation=org)
        self.assertEqual(org.api_keys.count(), 2)
        self.assertIn(first, org.api_keys.all())
        self.assertIn(second, org.api_keys.all())


class ApiKeyAuthenticateTests(TestCase):
    def test_valid_key_returns_instance_and_stamps_last_used(self):
        original, raw = ApiKey.issue(organisation=_org())
        self.assertIsNone(original.last_used_at)

        found = ApiKey.authenticate(raw)
        self.assertEqual(found, original)
        self.assertIsNotNone(found.last_used_at)

    def test_unknown_key_returns_none(self):
        ApiKey.issue(organisation=_org())
        self.assertIsNone(ApiKey.authenticate("mfk_totally-bogus-token"))

    def test_inactive_key_rejected(self):
        instance, raw = ApiKey.issue(organisation=_org())
        instance.is_active = False
        instance.save(update_fields=["is_active"])
        self.assertIsNone(ApiKey.authenticate(raw))

    def test_empty_input_returns_none(self):
        self.assertIsNone(ApiKey.authenticate(None))
        self.assertIsNone(ApiKey.authenticate(""))

    def test_authenticate_does_not_log_use_for_invalid_key(self):
        instance, _ = ApiKey.issue(organisation=_org())
        ApiKey.authenticate("mfk_wrong")
        instance.refresh_from_db()
        self.assertIsNone(instance.last_used_at)
