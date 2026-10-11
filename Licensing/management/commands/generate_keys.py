from django.core.management.base import BaseCommand
from Licensing.utils import LicenseKeyGenerator
import os


class Command(BaseCommand):
    help = 'Generate RSA key pair for license signing'

    def add_arguments(self, parser):
        parser.add_argument(
            '--output-dir',
            type=str,
            default='./keys',
            help='Directory to save keys'
        )
        parser.add_argument(
            '--key-size',
            type=int,
            default=2048,
            help='RSA key size (2048 or 4096)'
        )

    def handle(self, *args, **options):
        output_dir = options['output_dir']
        key_size = options['key_size']

        # Create output directory if it doesn't exist
        os.makedirs(output_dir, exist_ok=True)

        private_key_path = os.path.join(output_dir, 'private_key.pem')
        public_key_path = os.path.join(output_dir, 'public_key.pem')

        # Check if keys already exist
        if os.path.exists(private_key_path) or os.path.exists(public_key_path):
            self.stdout.write(self.style.WARNING('⚠ Keys already exist!'))
            if input('Overwrite existing keys? (yes/no): ').lower() != 'yes':
                self.stdout.write(self.style.ERROR('Cancelled.'))
                return

        self.stdout.write(f'Generating {key_size}-bit RSA key pair...')

        try:
            private_pem, public_pem = LicenseKeyGenerator.generate_key_pair(key_size=key_size)

            # Write private key
            with open(private_key_path, 'wb') as f:
                f.write(private_pem)
            os.chmod(private_key_path, 0o600)  # Restrict permissions

            # Write public key
            with open(public_key_path, 'wb') as f:
                f.write(public_pem)

            self.stdout.write(self.style.SUCCESS('\n✓ RSA Keys Generated Successfully!\n'))
            self.stdout.write(self.style.HTTP_INFO('=' * 70))
            self.stdout.write(f'Private Key: {self.style.HTTP_REDIRECT(private_key_path)}')
            self.stdout.write(f'Public Key:  {self.style.HTTP_REDIRECT(public_key_path)}')
            self.stdout.write(self.style.HTTP_INFO('=' * 70))

            self.stdout.write('\n⚠ IMPORTANT:')
            self.stdout.write('  • Keep private_key.pem SECURE and NEVER share it')
            self.stdout.write('  • Distribute public_key.pem to your application frontend')
            self.stdout.write('  • Store keys outside of version control\n')

        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Error generating keys: {e}'))
