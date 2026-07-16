"""
Django settings for the Medifinance CRM.
"""

import os
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


SECRET_KEY = os.getenv(
    "DJANGO_SECRET_KEY",
    "django-insecure-#))7bu@1hl6ve5h!o8#@fi)przrlwjzix4$*9b!3$w4mk!nrr4",
)

DEBUG = os.getenv("DJANGO_DEBUG", "1") == "1"

ALLOWED_HOSTS = [h.strip() for h in os.getenv("DJANGO_ALLOWED_HOSTS", "*").split(",") if h.strip()]

# Full origins (with scheme) trusted for CSRF — required for the Cloud Run
# domain in production, e.g. "https://*.run.app".
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()
]


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    "django.contrib.humanize",

    "guardian",
    "storages",

    "accounts",
    "crm",
]

SITE_ID = 1

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise serves static files in production (right after SecurityMiddleware).
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "medifinance.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "medifinance.wsgi.application"


# DATABASE_URL drives the connection in every environment. Locally it's unset,
# so we fall back to SQLite. In production set e.g.
#   postgres://USER:PASSWORD@/medifinance_prod?host=/cloudsql/PROJECT:REGION:INSTANCE
DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
        conn_health_checks=True,
    )
}


AUTH_USER_MODEL = "accounts.User"

AUTHENTICATION_BACKENDS = (
    "django.contrib.auth.backends.ModelBackend",
    "guardian.backends.ObjectPermissionBackend",
)

# guardian: don't create an anonymous user row; deny all anon object perms.
ANONYMOUS_USER_NAME = None

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"


AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


LANGUAGE_CODE = "en-gb"
TIME_ZONE = "Europe/London"
USE_I18N = True
USE_TZ = True


STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# In production WhiteNoise serves hashed, compressed static files. In dev we use
# the plain storage so `{% static %}` works without running collectstatic first.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}

# Media (uploaded documents). Local filesystem by default; private Cloud Storage
# bucket when GS_BUCKET_NAME is set (e.g. on Cloud Run). Files are never public —
# they're served through a permission-checked download view.
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

GS_BUCKET_NAME = os.getenv("GS_BUCKET_NAME", "")
if GS_BUCKET_NAME:
    STORAGES["default"] = {
        "BACKEND": "storages.backends.gcloud.GoogleCloudStorage",
        "OPTIONS": {
            "bucket_name": GS_BUCKET_NAME,
            "default_acl": None,        # private; relies on uniform bucket-level access
            "querystring_auth": False,  # we don't expose object URLs directly
        },
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# --- Production security -----------------------------------------------------
# Applied only when DEBUG is off so local dev over http is unaffected.
if not DEBUG:
    # Cloud Run terminates TLS and forwards the original scheme in this header.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000  # 1 year
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True


# Logging — Django's default LOGGING filters the console handler to DEBUG=True
# only, so production tracebacks go nowhere. Override with an unconditional
# stderr handler so unhandled exceptions reach Cloud Run logs.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{asctime} {levelname} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO"},
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        # Surface the actual exceptions from our own modules too.
        "crm": {"handlers": ["console"], "level": "INFO"},
        "accounts": {"handlers": ["console"], "level": "INFO"},
    },
}


# HubSpot — used to build deep-links from records to their HubSpot equivalents.
HUBSPOT_PORTAL_ID = os.getenv("HUBSPOT_PORTAL_ID", "3378161")


# Xero — credentials for the developer app (one app per environment). Without
# these, the Xero pages render but the Connect button is disabled.
XERO_CLIENT_ID = os.getenv("XERO_CLIENT_ID", "")
XERO_CLIENT_SECRET = os.getenv("XERO_CLIENT_SECRET", "")
XERO_SCOPES = (
    "openid profile email "
    "offline_access "
    "accounting.transactions "
    "accounting.contacts "
    "accounting.settings.read"
)


# Email — SMTP over SSL to the medi-finance.co.uk mail server (used in dev too).
# Password comes from EMAIL_HOST_PASSWORD; if it's empty we fall back to the
# console backend so local runs without credentials still work (emails print to
# the terminal instead of failing SMTP auth).
EMAIL_HOST = os.getenv("EMAIL_HOST", "mail.medi-finance.co.uk")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "465"))
EMAIL_USE_SSL = os.getenv("EMAIL_USE_SSL", "1") == "1"   # port 465 = implicit SSL/SMTPS
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "0") == "1"   # STARTTLS (e.g. Mailtrap on 2525); mutually exclusive with USE_SSL
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "info@medi-finance.co.uk")
# .strip() guards against the common gotcha of a trailing newline arriving via
# Secret Manager / pasted-in env values — SMTP servers will reject the auth.
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "").strip()
EMAIL_TIMEOUT = int(os.getenv("EMAIL_TIMEOUT", "10"))    # seconds

DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", f"Medifinance <{EMAIL_HOST_USER}>")
SERVER_EMAIL = DEFAULT_FROM_EMAIL

# Seconds during which a repeat POST /api/deals/ (same introducer + customer
# email) returns the existing deal instead of creating a new one. Defaults to
# 24 hours; dev sets 10 so repeated demo submissions create real deals.
API_DEAL_DEDUP_SECONDS = int(os.getenv("API_DEAL_DEDUP_SECONDS", "86400"))

# Per-key rolling limits on new (non-dedup'd) deals via POST /api/deals/.
# Production-safe defaults; dev sets them high for demos.
API_DEAL_RATE_LIMIT_HOUR = int(os.getenv("API_DEAL_RATE_LIMIT_HOUR", "5"))
API_DEAL_RATE_LIMIT_DAY = int(os.getenv("API_DEAL_RATE_LIMIT_DAY", "25"))

# Comma-separated list of staff addresses notified about new API-created deals.
NOTIFY_EMAILS = [
    e.strip() for e in os.getenv("NOTIFY_EMAILS", "mnthurling@gmail.com").split(",") if e.strip()
]

# Comma-separated list of accounts addresses notified when staff request a
# commission invoice from a deal.
ACCOUNTS_EMAILS = [
    e.strip() for e in os.getenv("ACCOUNTS_EMAILS", "mnthurling@gmail.com").split(",") if e.strip()
]

if EMAIL_HOST_PASSWORD:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
