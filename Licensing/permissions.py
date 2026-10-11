from rest_framework.permissions import BasePermission


class IsLicensingSupportUser(BasePermission):
    """Allow license support operations for superusers, admins, and managers."""

    message = 'You do not have permission to perform licensing renewal operations.'

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False

        if user.is_superuser:
            return True

        return getattr(user, 'role', None) in {'ADMIN', 'MANAGER'}
