from django.test import TestCase

from accounts.models import Role, User


class UserManagerTests(TestCase):
    def test_create_user_defaults_to_associate(self):
        user = User.objects.create_user(email="a@example.com", password="x")
        self.assertEqual(user.role, Role.ASSOCIATE)
        self.assertTrue(user.is_associate)
        self.assertTrue(user.is_staff_member)
        self.assertFalse(user.is_admin)
        self.assertFalse(user.is_customer)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_create_user_with_explicit_role(self):
        user = User.objects.create_user(
            email="c@example.com", password="x", role=Role.CUSTOMER,
        )
        self.assertTrue(user.is_customer)
        self.assertFalse(user.is_staff_member)

    def test_create_superuser_is_admin(self):
        user = User.objects.create_superuser(email="root@example.com", password="x")
        self.assertEqual(user.role, Role.ADMIN)
        self.assertTrue(user.is_admin)
        self.assertTrue(user.is_staff_member)
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)

    def test_email_required(self):
        with self.assertRaises(ValueError):
            User.objects.create_user(email="", password="x")

    def test_email_domain_normalised(self):
        user = User.objects.create_user(email="Mixed@Example.COM", password="x")
        # Django's BaseUserManager lowercases the domain portion.
        self.assertEqual(user.email, "Mixed@example.com")

    def test_password_is_hashed(self):
        user = User.objects.create_user(email="a@example.com", password="secret123")
        self.assertNotEqual(user.password, "secret123")
        self.assertTrue(user.check_password("secret123"))
