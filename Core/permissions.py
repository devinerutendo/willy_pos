from rest_framework.permissions import BasePermission, SAFE_METHODS


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