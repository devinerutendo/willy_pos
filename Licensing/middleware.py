from django.conf import settings
from django.http import JsonResponse

from .services import build_license_status, is_exempt_path, normalize_machine_fingerprint


WRITE_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}


def _is_restricted_mode_blocked(request_path: str, method: str) -> tuple[bool, str | None]:
    is_write = method in WRITE_METHODS

    if request_path.startswith('/api/accounts/users/') and is_write:
        return True, 'Add users'

    if request_path.startswith('/api/core/pos-terminals/') and is_write:
        return True, 'Add terminals'

    if request_path.startswith('/api/core/branches/') and is_write:
        return True, 'Add branches'

    if request_path.startswith('/api/core/backups/'):
        return True, 'Change system settings'

    if '/report' in request_path.lower() or request_path.startswith('/api/sales/reconciliations/'):
        return True, 'Access advanced reports'

    return False, None


def _is_emergency_mode_allowed(request_path: str, method: str) -> bool:
    # Always allow licensing/account/session endpoints and docs/static resources.
    if request_path.startswith('/api/licensing/') or request_path.startswith('/api/accounts/'):
        return True

    # Allow sales and returns operations needed for critical continuity.
    if request_path.startswith('/api/sales/sales/') or request_path.startswith('/api/sales/returns/'):
        return True

    # Allow sales item creation/update needed by sales flows.
    if request_path.startswith('/api/sales/sale-items/'):
        return True

    # View products: allow read-only access to product-related inventory resources.
    if request_path.startswith('/api/inventory/products/') or request_path.startswith('/api/inventory/variants/'):
        return method == 'GET'

    return False


class LicenseEnforcementMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not getattr(settings, 'LICENSE_ENFORCEMENT_ENABLED', False):
            return self.get_response(request)

        # Account bootstrap and auth endpoints must remain reachable without
        # machine fingerprint so first-run setup and login can proceed.
        if request.path.startswith('/api/accounts/'):
            return self.get_response(request)

        if is_exempt_path(request.path):
            return self.get_response(request)

        machine_fingerprint = request.META.get('HTTP_X_MACHINE_FINGERPRINT')
        if not machine_fingerprint:
            return JsonResponse({'detail': 'X-Machine-Fingerprint header is required.'}, status=428)

        machine_fingerprint = normalize_machine_fingerprint(machine_fingerprint)
        status_data = build_license_status(machine_fingerprint)
        if not status_data['has_license']:
            return JsonResponse(
                {
                    'detail': 'A valid machine registration is required before using the app.',
                    'machine_fingerprint': machine_fingerprint,
                },
                status=403,
            )

        license_state = status_data.get('license_state', 'none')
        method = request.method.upper()

        if license_state == 'restricted_mode':
            blocked, capability = _is_restricted_mode_blocked(request.path, method)
            if blocked:
                return JsonResponse(
                    {
                        'detail': 'Restricted mode: this action is blocked until the license is renewed.',
                        'license_state': license_state,
                        'blocked_capability': capability,
                        'license_state_display': status_data.get('license_state_display'),
                    },
                    status=403,
                )

        if license_state == 'emergency_mode' and not _is_emergency_mode_allowed(request.path, method):
            return JsonResponse(
                {
                    'detail': 'Emergency mode: only sales, returns, receipt printing, and product viewing are available.',
                    'license_state': license_state,
                    'license_state_display': status_data.get('license_state_display'),
                },
                status=403,
            )

        request.machine_fingerprint = machine_fingerprint
        request.license = status_data['license_obj']
        return self.get_response(request)