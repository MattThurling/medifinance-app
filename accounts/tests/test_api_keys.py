from django.test import TestCase

from accounts.models import ApiKey, User
from crm.models import Organisation


def _org(name: str = "Acme Brokers") -> Organisation:
    return Organisation.objects.create(name=name)


class ApiKeyIssueTests(TestCase):
    def test_issue_returns_instance_and_raw_key(self):
        creator = User.objects.create_user(email="staff@example.com", password="x")
        org = _org()
        key, raw = ApiKey.issue(organisation=org, name="Production", created_by=creator)

        self.assertIsNotNone(key.pk)
        self.assertEqual(key.organisation, org)
        self.assertEqual(key.name, "Production")
        self.assertEqual(key.created_by, creator)
        self.assertTrue(key.is_active)
        self.assertIsNone(key.last_used_at)
        # Raw key is recognisable — prefix lets us spot it in logs.
        self.assertTrue(raw.startswith("mfk_"))
        # Prefix on the model matches the first N chars of the raw key.
        self.assertTrue(raw.startswith(key.prefix))
        self.assertEqual(len(key.prefix), ApiKey.PREFIX_VISIBLE_LEN)

    def test_raw_key_is_never_stored(self):
        _, raw = ApiKey.issue(organisation=_org(), name="Production")
        # Hash is stored, not the raw key.
        self.assertNotIn(raw, ApiKey.objects.values_list("hashed_key", flat=True))

    def test_two_issued_keys_are_distinct(self):
        org = _org()
        _, a = ApiKey.issue(organisation=org, name="Production")
        _, b = ApiKey.issue(organisation=org, name="Sandbox")
        self.assertNotEqual(a, b)

    def test_keys_can_belong_to_same_organisation(self):
        """One integrator may want several keys (prod / sandbox / per-service)."""
        org = _org()
        prod, _ = ApiKey.issue(organisation=org, name="Production")
        sandbox, _ = ApiKey.issue(organisation=org, name="Sandbox")
        self.assertEqual(list(org.api_keys.order_by("name")), [prod, sandbox])


class ApiKeyAuthenticateTests(TestCase):
    def test_valid_key_returns_instance_and_stamps_last_used(self):
        original, raw = ApiKey.issue(organisation=_org(), name="Production")
        self.assertIsNone(original.last_used_at)

        found = ApiKey.authenticate(raw)
        self.assertEqual(found, original)
        self.assertIsNotNone(found.last_used_at)

    def test_unknown_key_returns_none(self):
        ApiKey.issue(organisation=_org(), name="Production")
        self.assertIsNone(ApiKey.authenticate("mfk_totally-bogus-token"))

    def test_inactive_key_rejected(self):
        instance, raw = ApiKey.issue(organisation=_org(), name="Production")
        instance.is_active = False
        instance.save(update_fields=["is_active"])
        self.assertIsNone(ApiKey.authenticate(raw))

    def test_empty_input_returns_none(self):
        self.assertIsNone(ApiKey.authenticate(None))
        self.assertIsNone(ApiKey.authenticate(""))

    def test_authenticate_does_not_log_use_for_invalid_key(self):
        instance, _ = ApiKey.issue(organisation=_org(), name="Production")
        ApiKey.authenticate("mfk_wrong")
        instance.refresh_from_db()
        self.assertIsNone(instance.last_used_at)
