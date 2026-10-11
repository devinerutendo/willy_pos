from django.apps import AppConfig


class LicensingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'Licensing'
    verbose_name = 'License Management'

    def ready(self):
        import Licensing.signals  # noqa
