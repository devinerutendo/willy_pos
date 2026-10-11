import base64
import hashlib
import json
import platform
import socket
import uuid
from datetime import timedelta
from typing import Dict, Optional, Tuple

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from django.conf import settings
from django.utils import timezone


class MachineFingerprint:
    """Generate and manage machine fingerprints for offline licensing"""

    @staticmethod
    def get_machine_fingerprint() -> str:
        """
        Generate a unique fingerprint for this machine based on hardware identifiers.
        Uses CPU, MAC address, and system info to create a stable identifier.
        """
        fingerprint_data = []

        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Cryptography') as key:
                machine_guid, _ = winreg.QueryValueEx(key, 'MachineGuid')
                fingerprint_data.append(('machine_guid', machine_guid.strip()))
        except Exception:
            fingerprint_data.append(('machine_guid', 'unknown'))

        try:
            mac = uuid.getnode()
            fingerprint_data.append(('mac', format(mac, '012x')))
        except Exception:
            fingerprint_data.append(('mac', 'unknown'))

        try:
            hostname = socket.gethostname()
            fingerprint_data.append(('hostname', hostname))
        except Exception:
            fingerprint_data.append(('hostname', 'unknown'))

        fingerprint_data.append(('system', platform.system()))
        fingerprint_data.append(('machine', platform.machine()))

        fingerprint_str = '|'.join([f'{key}:{value}' for key, value in fingerprint_data])
        return hashlib.sha256(fingerprint_str.encode('utf-8')).hexdigest()

    @staticmethod
    def get_machine_name() -> str:
        """Get friendly machine name (Windows computer name)"""
        try:
            return socket.gethostname()
        except Exception:
            return platform.node()


class LicenseKeyGenerator:
    """Generate and sign license keys using RSA cryptography"""

    def __init__(self, private_key_path: str = None, public_key_path: str = None):
        """
        Initialize with paths to RSA keys.
        If not provided, will look for keys in settings.
        """
        self.private_key_path = private_key_path or getattr(settings, 'LICENSE_PRIVATE_KEY_PATH', None)
        self.public_key_path = public_key_path or getattr(settings, 'LICENSE_PUBLIC_KEY_PATH', None)
        self._private_key = None
        self._public_key = None

    def _load_private_key(self):
        """Load private key from file"""
        if self._private_key:
            return self._private_key

        if not self.private_key_path:
            raise FileNotFoundError('Private key path is not configured.')

        try:
            with open(self.private_key_path, 'rb') as f:
                self._private_key = serialization.load_pem_private_key(
                    f.read(),
                    password=None,
                    backend=default_backend()
                )
            return self._private_key
        except FileNotFoundError:
            raise FileNotFoundError(f"Private key not found at {self.private_key_path}")

    def _load_public_key(self):
        """Load public key from file"""
        if self._public_key:
            return self._public_key

        if not self.public_key_path:
            raise FileNotFoundError('Public key path is not configured.')

        try:
            with open(self.public_key_path, 'rb') as f:
                self._public_key = serialization.load_pem_public_key(
                    f.read(),
                    backend=default_backend()
                )
            return self._public_key
        except FileNotFoundError:
            raise FileNotFoundError(f"Public key not found at {self.public_key_path}")

    @staticmethod
    def generate_key_pair(key_size: int = 2048) -> Tuple[bytes, bytes]:
        """
        Generate a new RSA key pair.
        Returns: (private_key_pem, public_key_pem)
        """
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=key_size,
            backend=default_backend()
        )
        public_key = private_key.public_key()

        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        )

        public_pem = public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        )

        return private_pem, public_pem

    def generate_license_key(self) -> str:
        """Generate a unique license key"""
        timestamp = int(timezone.now().timestamp())
        random_suffix = uuid.uuid4().hex[:8]
        return f"LIC-{timestamp}-{random_suffix}".upper()

    def create_license_data(
        self,
        license_key: str,
        plan: str,
        subscription_duration: str,
        machine_limit: Optional[int],
        customer_name: str = "",
        customer_email: str = ""
    ) -> Dict:
        """
        Create the license data dictionary that will be signed.
        """
        if subscription_duration == 'trial':
            expires_at = timezone.now() + timedelta(days=30)
        elif subscription_duration == '3_months':
            expires_at = timezone.now() + timedelta(days=90)
        elif subscription_duration == 'yearly':
            expires_at = timezone.now() + timedelta(days=365)
        else:  # lifetime
            expires_at = None

        license_data = {
            'license_key': license_key,
            'plan': plan,
            'subscription_duration': subscription_duration,
            'machine_limit': machine_limit,
            'customer_name': customer_name,
            'customer_email': customer_email,
            'issued_at': timezone.now().isoformat(),
            'expires_at': expires_at.isoformat() if expires_at else None,
        }

        return license_data

    def sign_license_data(self, license_data: Dict) -> str:
        """
        Sign the license data using the private key.
        Returns: Base64-encoded signature
        """
        private_key = self._load_private_key()

        # Create JSON representation for signing
        data_to_sign = json.dumps(license_data, sort_keys=True, separators=(',', ':'))

        signature = private_key.sign(
            data_to_sign.encode(),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH
            ),
            hashes.SHA256()
        )

        return base64.b64encode(signature).decode('utf-8')

    def verify_signature(self, license_data: Dict, signature: str) -> bool:
        """
        Verify a signed license.
        Returns: True if signature is valid, False otherwise
        """
        public_key = self._load_public_key()

        try:
            data_to_verify = json.dumps(license_data, sort_keys=True, separators=(',', ':'))
            signature_bytes = base64.b64decode(signature)

            public_key.verify(
                signature_bytes,
                data_to_verify.encode(),
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.MAX_LENGTH
                ),
                hashes.SHA256()
            )
            return True
        except Exception as e:
            print(f"Signature verification failed: {e}")
            return False

    def generate_signed_license(
        self,
        plan: str,
        subscription_duration: str,
        customer_name: str = "",
        customer_email: str = ""
    ) -> Tuple[str, Dict, str]:
        """
        Generate a complete signed license.
        Returns: (license_key, license_data, signature)
        """
        license_key = self.generate_license_key()
        max_machines = self._get_max_machines_for_plan(plan)

        license_data = self.create_license_data(
            license_key=license_key,
            plan=plan,
            subscription_duration=subscription_duration,
            machine_limit=max_machines,
            customer_name=customer_name,
            customer_email=customer_email
        )

        signature = self.sign_license_data(license_data)

        return license_key, license_data, signature

    @staticmethod
    def _get_max_machines_for_plan(plan: str) -> Optional[int]:
        """Map plan to machine count"""
        plans = {
            'free_trial': 1,
            'starter': 1,
            'business': 5,
            'enterprise': None,
        }
        return plans.get(plan, 1)
