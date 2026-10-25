from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission, SAFE_METHODS


def require_scope(user, scope, action='read'):
    """Raise 403 unless `user` holds `action` access on `scope`."""
    if not user or not user.is_authenticated or not user.has_permission_for(scope, action):
        raise PermissionDenied('You do not have permission to access this resource.')


def can_view_cost_data(user) -> bool:
    return bool(user and user.is_authenticated and user.can_view_cost_data())


class ScopePermission(BasePermission):
    """Role-aware permission gate keyed by a view-level permission scope."""

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False

        if user.is_superuser:
            return True

        scope = getattr(view, 'permission_scope', None)
        if not scope:
            return True

        action = 'read' if request.method in SAFE_METHODS else 'write'
        return user.has_permission_for(scope, action)