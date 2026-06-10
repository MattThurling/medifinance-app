import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone


class Role(models.TextChoices):
    ADMIN = "admin", "Admin"
    ASSOCIATE = "associate", "Associate"
    CUSTOMER = "customer", "Customer"


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("An email address is required.")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        extra_fields.setdefault("role", Role.ASSOCIATE)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("role", Role.ADMIN)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self._create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(unique=True)
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)

    role = models.CharField(max_length=16, choices=Role.choices, default=Role.ASSOCIATE)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)

    hubspot_id = models.CharField(max_length=64, blank=True, null=True, unique=True, db_index=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    objects = UserManager()

    class Meta:
        ordering = ["email"]

    def __str__(self) -> str:
        return self.email

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip() or self.email

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    @property
    def is_associate(self) -> bool:
        return self.role == Role.ASSOCIATE

    @property
    def is_customer(self) -> bool:
        return self.role == Role.CUSTOMER

    @property
    def is_staff_member(self) -> bool:
        """True for admins and associates — anyone who can use the internal UI."""
        return self.role in {Role.ADMIN, Role.ASSOCIATE}


class MagicLink(models.Model):
    """A single-use, time-limited login URL. Consuming it logs the `user`
    in and redirects to `redirect_url`.

    Use the `MagicLink.issue(...)` classmethod rather than constructing
    directly — it handles token generation and expiry.
    """

    DEFAULT_TTL_DAYS = 7

    token = models.CharField(max_length=64, unique=True, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="magic_links",
    )
    redirect_url = models.CharField(
        max_length=512,
        help_text="Path the customer lands on after consuming the link, e.g. /portal/deals/123/",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="issued_magic_links",
    )
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()

    used_at = models.DateTimeField(null=True, blank=True)
    used_ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        status = "used" if self.used_at else ("expired" if self.is_expired else "active")
        return f"MagicLink to {self.user} ({status})"

    @classmethod
    def issue(cls, *, user, redirect_url: str, created_by=None, ttl_days: int = DEFAULT_TTL_DAYS) -> "MagicLink":
        return cls.objects.create(
            token=secrets.token_urlsafe(32),
            user=user,
            redirect_url=redirect_url,
            created_by=created_by,
            expires_at=timezone.now() + timedelta(days=ttl_days),
        )

    @property
    def is_consumed(self) -> bool:
        return self.used_at is not None

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @property
    def is_valid(self) -> bool:
        return not self.is_consumed and not self.is_expired

    def consume(self, *, ip: str | None = None) -> None:
        self.used_at = timezone.now()
        self.used_ip = ip
        self.save(update_fields=["used_at", "used_ip"])


class ApiKey(models.Model):
    """Bearer token for the public quote API.

    Integrators send `Authorization: Bearer <raw_key>`; we hash the incoming
    value and look it up against `hashed_key`. The raw key is only ever shown
    once — at issue time, via a flash message in admin — and never stored.
    """

    KEY_PREFIX = "mfk_"          # "Medifinance key" — identifiable in logs
    PREFIX_VISIBLE_LEN = 12      # `mfk_` + first 8 chars of the secret

    organisation = models.ForeignKey(
        "crm.Organisation",
        on_delete=models.PROTECT,
        related_name="api_keys",
        help_text="The partner organisation this key belongs to.",
    )
    prefix = models.CharField(
        max_length=16,
        db_index=True,
        help_text="First 12 chars of the key — identifies it in logs and "
                  "admin without exposing the secret.",
    )
    hashed_key = models.CharField(max_length=64, unique=True, db_index=True)
    is_active = models.BooleanField(
        default=True,
        help_text="Untick to revoke without deleting (preserves audit history).",
    )
    created_at = models.DateTimeField(default=timezone.now)
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="issued_api_keys",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        status = "active" if self.is_active else "revoked"
        return f"{self.organisation.name} ({self.prefix}… · {status})"

    @classmethod
    def _hash(cls, raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    @classmethod
    def issue(cls, *, organisation, created_by=None) -> tuple["ApiKey", str]:
        """Mint a new key for `organisation`. Returns ``(instance, raw_key)`` —
        the raw key is the only time the secret is ever surfaced; store it
        somewhere safe before navigating away."""
        raw = cls.KEY_PREFIX + secrets.token_urlsafe(32)
        instance = cls.objects.create(
            organisation=organisation,
            prefix=raw[:cls.PREFIX_VISIBLE_LEN],
            hashed_key=cls._hash(raw),
            created_by=created_by,
        )
        return instance, raw

    @classmethod
    def authenticate(cls, raw_key: str | None) -> "ApiKey | None":
        """Look up an active key by its raw token. Stamps `last_used_at` on a
        match. Returns None on miss / revoked / bad input."""
        if not raw_key:
            return None
        try:
            key = cls.objects.get(hashed_key=cls._hash(raw_key), is_active=True)
        except cls.DoesNotExist:
            return None
        key.touch()
        return key

    def touch(self) -> None:
        self.last_used_at = timezone.now()
        self.save(update_fields=["last_used_at"])
