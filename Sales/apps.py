from django.apps import AppConfig


class SalesConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'Sales'
    
    def ready(self):
        """Register signal handlers when app is ready."""
        import Sales.signals
