from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from datetime import timedelta
from Licensing.models import License
from Licensing.utils import LicenseKeyGenerator
import json


class Command(BaseCommand):
    help = 'Generate a new license key for a customer'

    def add_arguments(self, parser):
        parser.add_argument(
            '--plan',
            type=str,
            choices=['free_trial', 'starter', 'business', 'enterprise'],
            required=True,
            help='License plan type'
        )
        parser.add_argument(
            '--duration',
            type=str,
            choices=['trial', '3_months', 'yearly', 'lifetime'],
            required=True,
            help='Subscription duration'
        )
        parser.add_argument(
            '--customer-name',
            type=str,
            default='',
            help='Customer name'
        )
        parser.add_argument(
            '--customer-email',
            type=str,
            default='',
            help='Customer email'
        )
        parser.add_argument(
            '--private-key',
            type=str,
            default='./keys/private_key.pem',
            help='Path to private RSA key'
        )

    def handle(self, *args, **options):
        plan = options['plan']
        duration = options['duration']
        customer_name = options['customer_name']
        customer_email = options['customer_email']
        private_key_path = options['private_key']

        if plan == 'free_trial' and duration != 'trial':
            raise CommandError("Free Trial plan only supports 'trial' (1 month) duration")
        if plan in {'starter', 'business', 'enterprise'} and duration not in {'3_months', 'yearly', 'lifetime'}:
            raise CommandError('Paid plans only support 3_months, yearly, or lifetime subscriptions.')

        try:
            generator = LicenseKeyGenerator(
                private_key_path=private_key_path,
                public_key_path=private_key_path.replace('private', 'public')
            )

            license_key, license_data, signature = generator.generate_signed_license(
                plan=plan,
                subscription_duration=duration,
                customer_name=customer_name,
                customer_email=customer_email
            )

            # Calculate expiration date
            if duration == 'trial':
                expires_at = timezone.now() + timedelta(days=30)
            elif duration == '3_months':
                expires_at = timezone.now() + timedelta(days=90)
            elif duration == 'yearly':
                expires_at = timezone.now() + timedelta(days=365)
            else:  # lifetime
                expires_at = None

            max_machines_map = {
                'free_trial': 1,
                'starter': 1,
                'business': 5,
                'enterprise': None,
            }
            max_machines = max_machines_map.get(plan, 1)

            license_obj = License.objects.create(
                license_key=license_key,
                plan=plan,
                subscription_duration=duration,
                machine_limit=max_machines,
                license_payload=license_data,
                expires_at=expires_at,
                signature=signature,
                customer_name=customer_name,
                customer_email=customer_email,
                is_active=True
            )

            # Display success message
            self.stdout.write(self.style.SUCCESS('\n✓ License Generated Successfully!\n'))
            self.stdout.write(self.style.HTTP_INFO('=' * 70))
            self.stdout.write(f"License Key:        {self.style.HTTP_REDIRECT(license_key)}")
            self.stdout.write(f"Plan:               {self.style.HTTP_INFO(license_obj.get_plan_display())}")
            self.stdout.write(f"Duration:           {self.style.HTTP_INFO(license_obj.get_subscription_duration_display())}")
            self.stdout.write(f"Max Machines:       {self.style.HTTP_INFO(license_obj.machine_limit_display)}")
            if expires_at:
                self.stdout.write(f"Expires:            {self.style.WARNING(expires_at.strftime('%Y-%m-%d %H:%M:%S'))}")
            else:
                self.stdout.write(f"Expires:            {self.style.SUCCESS('Never (Lifetime)')}")
            if customer_name:
                self.stdout.write(f"Customer:           {customer_name}")
            if customer_email:
                self.stdout.write(f"Email:              {customer_email}")
            self.stdout.write(self.style.HTTP_INFO('=' * 70))

            self.stdout.write(f"\nShare this license key with the customer:")
            self.stdout.write(self.style.SUCCESS(f"    {license_key}\n"))

            # Also output the full license data as JSON for offline distribution
            self.stdout.write("License Data (for offline distribution):")
            self.stdout.write(json.dumps({
                'license_key': license_key,
                'license_data': license_data,
                'signature': signature
            }, indent=2))

        except FileNotFoundError as e:
            raise CommandError(f"Error loading RSA keys: {e}")
        except Exception as e:
            raise CommandError(f"Error generating license: {e}")
