from django.core.exceptions import ValidationError
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .permissions import IsLicensingSupportUser
from .serializers import (
    LicenseActivationSerializer,
    LicenseRenewalAuditSerializer,
    LicenseSerializer,
    LicenseStatusSerializer,
    ManualExpiryExtensionSerializer,
    MachineRegistrationSerializer,
    SignedRenewalSerializer,
    TrialGenerationSerializer,
)
from .services import (
    activate_purchased_license,
    apply_signed_license_renewal,
    build_license_status,
    create_trial_license,
    manually_extend_license_expiry,
    normalize_machine_fingerprint,
    normalize_machine_name,
)
from .utils import MachineFingerprint


class LicenseViewSet(viewsets.ViewSet):
    """Handle license operations"""

    def get_permissions(self):
        if self.action in ['activate_license', 'get_trial', 'status']:
            return [AllowAny()]
        if self.action in ['apply_signed_renewal', 'extend_expiry', 'renewal_audits']:
            return [IsAuthenticated(), IsLicensingSupportUser()]
        return [IsAuthenticated()]

    @action(detail=False, methods=['post'], permission_classes=[AllowAny])
    def get_trial(self, request):
        """
        Generate and activate a free trial license for this machine.
        Called on first app launch.
        """
        try:
            machine_fingerprint = normalize_machine_fingerprint(request.data.get('machine_fingerprint'))
            machine_name = normalize_machine_name(request.data.get('machine_name'))
            license_obj, registration, created = create_trial_license(machine_fingerprint, machine_name)

            return Response({
                'license_key': license_obj.license_key,
                'plan': license_obj.get_plan_display(),
                'machine_fingerprint': machine_fingerprint,
                'days_remaining': license_obj.days_remaining,
                'expires_at': license_obj.expires_at,
                'registered_machine': MachineRegistrationSerializer(registration).data,
                'created': created,
                'message': 'Free trial license activated. 30 days remaining.'
            }, status=status.HTTP_201_CREATED)

        except ValidationError as e:
            message = e.messages[0] if getattr(e, 'messages', None) else str(e)
            return Response({'error': message}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response(
                {'error': f'Failed to create trial license: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['post'], permission_classes=[AllowAny])
    def activate_license(self, request):
        """
        Activate a purchased license on this machine.
        """
        serializer = LicenseActivationSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        license_key = serializer.validated_data['license_key']
        machine_fingerprint = serializer.validated_data['machine_fingerprint']
        machine_name = serializer.validated_data.get('machine_name') or MachineFingerprint.get_machine_name()
        signature = serializer.validated_data['signature']
        license_data = serializer.validated_data['license_data']

        try:
            license_obj, machine_reg, created = activate_purchased_license(
                license_key=license_key,
                license_data=license_data,
                signature=signature,
                machine_fingerprint=machine_fingerprint,
                machine_name=machine_name,
            )

            return Response({
                'message': 'License activated successfully',
                'license': LicenseSerializer(license_obj).data,
                'machine': MachineRegistrationSerializer(machine_reg).data,
                'created': created,
            }, status=status.HTTP_200_OK)

        except ValidationError as e:
            message = e.messages[0] if getattr(e, 'messages', None) else str(e)
            return Response({'error': message}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response(
                {'error': f'Activation failed: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['post'], permission_classes=[AllowAny])
    def status(self, request):
        """
        Get current license status for a machine.
        """
        machine_fingerprint = normalize_machine_fingerprint(request.data.get('machine_fingerprint'))
        status_data = build_license_status(machine_fingerprint)

        if not status_data['has_license']:
            return Response({
                'has_license': False,
                'license': None,
                'registered_machines': [],
                'trial_active': False,
                'can_add_machines': False,
                'machines_used': 0,
                'machines_available': None,
                'machine_fingerprint': machine_fingerprint,
                'license_state': 'none',
                'license_state_display': 'No Active License',
                'allowed_capabilities': [],
                'blocked_capabilities': [],
                'message': 'No active license. Please activate or start free trial.'
            })

        return Response({
            'has_license': status_data['has_license'],
            'license': LicenseSerializer(status_data['license_obj']).data if status_data['license_obj'] else None,
            'registered_machines': MachineRegistrationSerializer(status_data['registered_machines'], many=True).data,
            'trial_active': status_data['trial_active'],
            'can_add_machines': status_data['can_add_machines'],
            'machines_used': status_data['machines_used'],
            'machines_available': status_data['machines_available'],
            'machine_fingerprint': machine_fingerprint,
            'license_state': status_data['license_state'],
            'license_state_display': status_data['license_state_display'],
            'allowed_capabilities': status_data['allowed_capabilities'],
            'blocked_capabilities': status_data['blocked_capabilities'],
        })

    @action(detail=False, methods=['post'], permission_classes=[IsAuthenticated, IsLicensingSupportUser])
    def apply_signed_renewal(self, request):
        serializer = SignedRenewalSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            license_obj = apply_signed_license_renewal(
                license_key=serializer.validated_data['license_key'],
                renewal_payload=serializer.validated_data['renewal_payload'],
                signature=serializer.validated_data['signature'],
                actor=serializer.validated_data.get('actor', ''),
                machine_fingerprint=serializer.validated_data.get('machine_fingerprint', ''),
            )
            return Response(
                {
                    'message': 'Signed renewal applied successfully.',
                    'license': LicenseSerializer(license_obj).data,
                },
                status=status.HTTP_200_OK,
            )
        except ValidationError as e:
            message = e.messages[0] if getattr(e, 'messages', None) else str(e)
            return Response({'error': message}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({'error': f'Renewal failed: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'], permission_classes=[IsAuthenticated, IsLicensingSupportUser])
    def extend_expiry(self, request):
        serializer = ManualExpiryExtensionSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            license_obj = manually_extend_license_expiry(
                license_key=serializer.validated_data['license_key'],
                new_expires_at=serializer.validated_data['new_expires_at'],
                reason=serializer.validated_data['reason'],
                actor=serializer.validated_data['actor'],
                machine_fingerprint=serializer.validated_data.get('machine_fingerprint', ''),
            )
            return Response(
                {
                    'message': 'License expiry extended and audited successfully.',
                    'license': LicenseSerializer(license_obj).data,
                },
                status=status.HTTP_200_OK,
            )
        except ValidationError as e:
            message = e.messages[0] if getattr(e, 'messages', None) else str(e)
            return Response({'error': message}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({'error': f'Extension failed: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['get'], permission_classes=[IsAuthenticated, IsLicensingSupportUser])
    def renewal_audits(self, request):
        license_key = request.query_params.get('license_key', '').strip()
        if not license_key:
            return Response({'error': 'license_key query parameter is required.'}, status=status.HTTP_400_BAD_REQUEST)

        from .models import LicenseRenewalAudit

        audits = LicenseRenewalAudit.objects.filter(license__license_key=license_key).order_by('-created_at')[:100]
        return Response(
            {
                'license_key': license_key,
                'count': audits.count(),
                'results': LicenseRenewalAuditSerializer(audits, many=True).data,
            },
            status=status.HTTP_200_OK,
        )
