from .audit import set_current_user


class AuditUserMiddleware:
    """Expose the authenticated user to audit signal handlers."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        set_current_user(user if getattr(user, 'is_authenticated', False) else None)
        try:
            return self.get_response(request)
        finally:
            set_current_user(None)