from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'accounts'

    def ready(self):
        # Registers the TWO_FACTOR_REQUIRED_ROLES system check.
        from . import two_factor  # noqa: F401
