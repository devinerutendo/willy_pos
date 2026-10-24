from datetime import timedelta

from django.utils import timezone
from rest_framework import exceptions
from rest_framework.authentication import TokenAuthentication

# Avoid a database write on every request.
ACTIVITY_TOUCH_INTERVAL = timedelta(seconds=30)


class ActivityTokenAuthentication(TokenAuthentication):
    """
    Token authentication that enforces each user's inactivity timeout.

    When ``session_timeout_enabled`` is on and the token has been idle for
    longer than ``session_timeout_minutes`` it is revoked and the request is
    rejected with 401, so the limit cannot be bypassed from the client.
    """

    def authenticate_credentials(self, key):
        user, token = super().authenticate_credentials(key)

        from .models import UserSecurity  # imported lazily: app registry

        security = UserSecurity.objects.filter(user=user).first()
        if security is None:
            return user, token

        now = timezone.now()
        if security.session_timeout_minutes is not None and security.last_activity_at:
            idle_limit = timedelta(minutes=security.session_timeout_minutes)
            if now - security.last_activity_at > idle_limit:
                token.delete()
                raise exceptions.AuthenticationFailed('Session expired due to inactivity.')

        if (
            security.last_activity_at is None
            or now - security.last_activity_at > ACTIVITY_TOUCH_INTERVAL
        ):
            UserSecurity.objects.filter(pk=security.pk).update(last_activity_at=now)

        return user, token
