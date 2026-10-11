from django.contrib.auth.models import AbstractUser
from django.db import models
from django.core.exceptions import ValidationError


class User(AbstractUser):
    """
    Extended user model with role-based access control.
    Supports manager and sales staff roles with appropriate permissions.
    """
    ADMIN = "ADMIN"
    MANAGER = "MANAGER"
    INVENTORY = "INVENTORY"
    SALES = "SALES"
    AUDITOR = "AUDITOR"

    ROLE_CHOICES = (
        (ADMIN, "Administrator - Full Platform Access"),
        (MANAGER, "Manager - Full System Access"),
        (INVENTORY, "Inventory Controller - Stock & Procurement Rights"),
        (SALES, "Salesperson - POS & Transaction Rights"),
        (AUDITOR, "Auditor - Read-only Audit Access"),
    )

    ROLE_PERMISSION_MATRIX = {
        ADMIN: {
            '*': 'write',
        },
        MANAGER: {
            'users': 'write',
            'branches': 'write',
            'pos': 'write',
            'inventory': 'write',
            'stock_adjustments': 'write',
            'sales': 'write',
            'audit': 'read',
        },
        INVENTORY: {
            'branches': 'read',
            'pos': 'read',
            'inventory': 'write',
            'stock_adjustments': 'write',
            'sales': 'read',
            'audit': 'read',
        },
        SALES: {
            'branches': 'read',
            'pos': 'read',
            'inventory': 'read',
            'stock_adjustments': 'read',
            'sales': 'write',
            'audit': 'read',
        },
        AUDITOR: {
            'branches': 'read',
            'pos': 'read',
            'inventory': 'read',
            'stock_adjustments': 'read',
            'sales': 'read',
            'audit': 'read',
        },
    }

    email = models.EmailField(
        unique=True,
        db_index=True,
        help_text="Email address (used for login)"
    )
    phone = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        help_text="Contact phone number"
    )
    role = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        default=SALES,
        db_index=True,
        help_text="User role determining system permissions"
    )
    employee_id = models.CharField(
        max_length=50,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
        help_text="Employee identification number"
    )
    branch = models.ForeignKey(
        'Core.Branch',
        on_delete=models.SET_NULL,
        related_name='users',
        null=True,
        blank=True,
        help_text="Operational branch this user belongs to"
    )
    profile_image = models.ImageField(
        upload_to='profiles/',
        null=True,
        blank=True,
        help_text="Optional profile image"
    )

    class Meta:
        verbose_name = 'User'
        verbose_name_plural = 'Users'
        indexes = [
            models.Index(fields=['email']),
            models.Index(fields=['role']),
            models.Index(fields=['is_active']),
        ]

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username', 'first_name', 'last_name']

    @property
    def is_manager(self) -> bool:
        """Check if user has manager privileges."""
        return self.role in {self.ADMIN, self.MANAGER}

    @property
    def is_admin(self) -> bool:
        return self.role == self.ADMIN

    @property
    def permission_matrix(self) -> dict:
        return self.ROLE_PERMISSION_MATRIX.get(self.role, {})

    def has_permission_for(self, scope: str, action: str = 'read') -> bool:
        if self.is_superuser:
            return True

        access = self.permission_matrix.get(scope, self.permission_matrix.get('*'))
        if access == 'write':
            return True
        if access == 'read' and action == 'read':
            return True
        return False

    @property
    def full_name(self) -> str:
        """Return user's full name."""
        return f"{self.first_name} {self.last_name}".strip()

    def clean(self):
        """Validate user constraints."""
        super().clean()
        if not self.first_name or not self.last_name:
            raise ValidationError(
                "First name and last name are required"
            )

    def __str__(self):
        return f"{self.full_name} ({self.email})"
