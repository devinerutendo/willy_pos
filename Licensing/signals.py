from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import License
from django.utils import timezone


@receiver(post_save, sender=License)
def log_license_change(sender, instance, created, **kwargs):
    """Log license creation or updates"""
    if created:
        print(f"[LICENSING] New license created: {instance.license_key} ({instance.plan})")
    else:
        if instance.activated_at and not License.objects.filter(
            pk=instance.pk, activated_at__isnull=True
        ):
            print(f"[LICENSING] License activated: {instance.license_key}")
