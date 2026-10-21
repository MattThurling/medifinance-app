import hashlib
import secrets
import time
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
    is_finance = models.BooleanField(
        default=False,
        help_text=(
            "Grants access to the Xero integration. "
            "Only takes effect for staff (admin/associate)."
        ),
    )
    organisation = models.ForeignKey(
        "crm.Organisation",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="members",
        help_text=(
            "The organisation this user works for. Associates in the same "
            "organisation can see and work on each other's deals."
        ),
    )

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)

    hubspot_id = models.CharField(max_length=64, blank=True, null=True, unique=True, db_index=True)

    # Two-factor authentication (TOTP). `totp_secret` is set during enrolment
    # and only counts once `totp_confirmed_at` is stamped by a correct code.
    # `totp_last_used_step` stops a sniffed code being replayed inside its
    # 30-second window.
    totp_secret = models.CharField(max_length=64, blank=True, editable=False)
    totp_confirmed_at = models.DateTimeField(null=True, blank=True, editable=False)
    totp_last_used_step = models.BigIntegerField(null=True, blank=True, editable=False)

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

    @property
    def is_finance_member(self) -> bool:
        """True for staff who also hold the finance flag — the gate for
        everything Xero. Strict: admins without the flag are excluded too."""
        return self.is_staff_member and self.is_finance

    # --- Two-factor authentication -----------------------------------------

    TOTP_STEP_SECONDS = 30
    TOTP_VALID_WINDOW = 1  # accept the previous/next step too (clock drift)

    @property
    def two_factor_enabled(self) -> bool:
        """The user has finished enrolling an authenticator app."""
        return bool(self.totp_secret) and self.totp_confirmed_at is not None

    @property
    def two_factor_required(self) -> bool:
        """This user's role is listed in TWO_FACTOR_REQUIRED_ROLES."""
        roles = settings.TWO_FACTOR_REQUIRED_ROLES
        return "all" in roles or self.role in roles

    @property
    def two_factor_needed(self) -> bool:
        """A code must be entered before this user can use the app: either
        the role demands it or the user has opted in."""
        return self.two_factor_required or self.two_factor_enabled

    @staticmethod
    def generate_totp_secret() -> str:
        import pyotp
        return pyotp.random_base32()

    def totp_provisioning_uri(self, secret: str | None = None) -> str:
        """otpauth:// URI the authenticator app scans (as a QR code)."""
        import pyotp
        return pyotp.TOTP(secret or self.totp_secret).provisioning_uri(
            name=self.email, issuer_name=settings.TWO_FACTOR_ISSUER,
        )

    @classmethod
    def _match_totp(cls, secret: str, code: str, *, now: float | None = None) -> int | None:
        """Return the time-step the code matches, or None. Checks the current
        step plus TOTP_VALID_WINDOW steps either side for clock drift."""
        import pyotp
        code = "".join(ch for ch in str(code) if ch.isdigit())
        if len(code) != 6:
            return None
        now = time.time() if now is None else now
        totp = pyotp.TOTP(secret, interval=cls.TOTP_STEP_SECONDS)
        step = int(now) // cls.TOTP_STEP_SECONDS
        for offset in range(-cls.TOTP_VALID_WINDOW, cls.TOTP_VALID_WINDOW + 1):
            candidate = step + offset
            if pyotp.utils.strings_equal(totp.generate_otp(candidate), code):
                return candidate
        return None

    def verify_totp(self, code: str, *, now: float | None = None) -> bool:
        """Check a login code against the confirmed secret. A code is single
        use: anything at or before the last accepted step is rejected."""
        if not self.two_factor_enabled:
            return False
        matched = self._match_totp(self.totp_secret, code, now=now)
        if matched is None:
            return False
        if self.totp_last_used_step is not None and matched <= self.totp_last_used_step:
            return False
        self.totp_last_used_step = matched
        self.save(update_fields=["totp_last_used_step"])
        return True

    def confirm_totp(self, secret: str, code: str, *, now: float | None = None) -> bool:
        """Finish enrolment: the user proved they have the secret in their
        app by entering a code for it. Returns False (and saves nothing) on a
        wrong code."""
        matched = self._match_totp(secret, code, now=now)
        if matched is None:
            return False
        self.totp_secret = secret
        self.totp_confirmed_at = timezone.now()
        self.totp_last_used_step = matched
        self.save(update_fields=["totp_secret", "totp_confirmed_at", "totp_last_used_step"])
        return True

    def reset_totp(self) -> None:
        """Remove the authenticator. If the role requires 2FA the user is
        forced to enrol again at their next request."""
        self.totp_secret = ""
        self.totp_confirmed_at = None
        self.totp_last_used_step = None
        self.save(update_fields=["totp_secret", "totp_confirmed_at", "totp_last_used_step"])


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
        help_text="Path the customer lands on after consuming the link, e.g. / (their dashboard).",
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
    is_sandbox = models.BooleanField(
        default=False,
        help_text="Sandbox key: POST /api/deals/ runs full validation and "
                  "returns a fake reference, but creates no deal and sends no "
                  "email. Issue one per partner for integration testing.",
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
        if self.is_sandbox:
            status += " · sandbox"
        return f"{self.organisation.name} ({self.prefix}… · {status})"

    @classmethod
    def _hash(cls, raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    @classmethod
    def issue(cls, *, organisation, created_by=None,
              is_sandbox: bool = False) -> tuple["ApiKey", str]:
        """Mint a new key for `organisation`. Returns ``(instance, raw_key)`` —
        the raw key is the only time the secret is ever surfaced; store it
        somewhere safe before navigating away."""
        raw = cls.KEY_PREFIX + secrets.token_urlsafe(32)
        instance = cls.objects.create(
            organisation=organisation,
            prefix=raw[:cls.PREFIX_VISIBLE_LEN],
            hashed_key=cls._hash(raw),
            created_by=created_by,
            is_sandbox=is_sandbox,
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


class SiteSettings(models.Model):
    """Single-row global settings. Use `SiteSettings.get()` to read or update.

    Currently holds the API kill switch only — flipping `api_enabled` off
    immediately blocks `POST /api/deals/` for every partner. Intended as a
    panic button for token compromise; individual partners can still be
    revoked via `ApiKey.is_active`.
    """

    SINGLETON_ID = 1

    api_enabled = models.BooleanField(
        default=True,
        help_text="When off, the partner deal-create API returns 503 for every "
                  "request. Per-key `ApiKey.is_active` still applies independently.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Site settings"
        verbose_name_plural = "Site settings"

    def save(self, *args, **kwargs):
        self.pk = self.SINGLETON_ID
        super().save(*args, **kwargs)

    @classmethod
    def get(cls) -> "SiteSettings":
        obj, _ = cls.objects.get_or_create(pk=cls.SINGLETON_ID)
        return obj
