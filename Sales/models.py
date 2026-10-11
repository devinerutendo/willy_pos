from django.db import models
from django.core.exceptions import ValidationError
from django.utils import timezone
from decimal import Decimal

from Core.models import BaseModel, BaseSyncModel
from Accounts.models import User
from Inventory.models import Product


class Customer(BaseSyncModel):
    """
    Customer entity for tracking client information.
    Supports both retail and wholesale customers.
    """
    name = models.CharField(
        max_length=255,
        db_index=True,
        help_text="Customer name"
    )
    email = models.EmailField(
        unique=True,
        help_text="Email address (unique per customer)"
    )
    phone = models.CharField(
        max_length=20,
        help_text="Contact phone number"
    )
    address = models.TextField(
        help_text="Delivery address"
    )
    city = models.CharField(
        max_length=100,
        blank=True,
        help_text="City name"
    )
    tax_id = models.CharField(
        max_length=50,
        blank=True,
        help_text="Tax identification number (for B2B)"
    )
    credit_limit = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Maximum credit allowed for customer"
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Whether customer can place orders"
    )

    class Meta:
        ordering = ['name']
        indexes = [
            models.Index(fields=['name']),
            models.Index(fields=['email']),
            models.Index(fields=['is_active']),
        ]

    def clean(self):
        """Validate customer constraints."""
        if self.credit_limit < 0:
            raise ValidationError("Credit limit cannot be negative")

    @property
    def total_spent(self) -> Decimal:
        """Calculate total lifetime customer spending."""
        return sum(
            Decimal(str(sale.total_amount)) for sale in self.sales.all()
        ) or Decimal('0.00')

    def __str__(self):
        return f"{self.name} ({self.email})"


class Quotation(BaseSyncModel):
    """
    Sales quotation - formal price quote for customer.
    Required step before order conversion.
    """
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"

    STATUS_CHOICES = (
        (PENDING, "Awaiting Customer Decision"),
        (ACCEPTED, "Customer Accepted"),
        (REJECTED, "Customer Rejected"),
        (EXPIRED, "Quote Expired"),
    )

    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        related_name='quotations',
        help_text="Customer receiving quotation"
    )
    branch = models.ForeignKey(
        'Core.Branch',
        on_delete=models.PROTECT,
        related_name='quotations',
        null=True,
        blank=True,
        help_text="Branch where the quotation was created"
    )
    pos_terminal = models.ForeignKey(
        'Core.POSTerminal',
        on_delete=models.SET_NULL,
        related_name='quotations',
        null=True,
        blank=True,
        help_text="POS terminal that captured the quotation"
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='quotations_created',
        help_text="User creating quotation"
    )
    reference = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
        help_text="Quotation reference number"
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=PENDING,
        db_index=True,
        help_text="Current quotation status"
    )
    valid_until = models.DateField(
        help_text="Quotation expiration date"
    )
    notes = models.TextField(
        blank=True,
        help_text="Terms, conditions, or special notes"
    )
    is_converted = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Whether quotation was converted to sale"
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['branch', 'created_at']),
            models.Index(fields=['customer', 'status']),
            models.Index(fields=['created_at']),
            models.Index(fields=['is_converted']),
        ]

    @property
    def total_amount(self) -> Decimal:
        """Calculate total quotation value."""
        return sum(
            Decimal(str(item.line_total))
            for item in self.items.all()
        ) or Decimal('0.00')

    @property
    def is_expired(self) -> bool:
        """Check if quotation has passed valid_until date."""
        return timezone.now().date() > self.valid_until

    def clean(self):
        """Validate quotation constraints."""
        if self.valid_until < timezone.now().date():
            raise ValidationError("Quotation expiration date cannot be in the past")

    def __str__(self):
        return f"Quote {self.reference} - {self.customer.name}"


class QuotationItem(models.Model):
    client_id = models.UUIDField(unique=True, null=True, blank=True, db_index=True)
    """
    Line items for a quotation.
    Normalized many-to-many between Quotation and Product.
    """
    quotation = models.ForeignKey(
        Quotation,
        on_delete=models.CASCADE,
        related_name='items',
        help_text="Parent quotation"
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name='quoted_items',
        help_text="Product being quoted"
    )
    quantity = models.PositiveIntegerField(
        help_text="Quantity quoted"
    )
    unit_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Unit price quoted to customer"
    )
    discount_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        help_text="Discount percentage (0-100%, admin only)"
    )
    discount_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Fixed discount amount per unit (admin only)"
    )

    class Meta:
        unique_together = ("quotation", "product")
        indexes = [
            models.Index(fields=['quotation', 'product']),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(unit_price__gte=0),
                name='quotation_unit_price_non_negative'
            ),
            models.CheckConstraint(
                condition=models.Q(discount_percentage__gte=0, discount_percentage__lte=100),
                name='quotation_discount_percentage_valid'
            ),
        ]

    def clean(self):
        """Validate quotation item constraints."""
        if self.quantity <= 0:
            raise ValidationError("Quantity must be greater than 0")
        if self.unit_price < 0:
            raise ValidationError("Unit price cannot be negative")
        if self.discount_percentage < 0 or self.discount_percentage > 100:
            raise ValidationError("Discount percentage must be between 0 and 100")
        if self.discount_amount < 0:
            raise ValidationError("Discount amount cannot be negative")

    @property
    def effective_unit_price(self) -> Decimal:
        """Calculate unit price after discounts."""
        base_price = Decimal(str(self.unit_price))
        discount_amt = Decimal(str(self.discount_amount))
        discount_pct = Decimal(str(self.discount_percentage))
        
        # Apply percentage discount first, then fixed amount
        price_after_pct = base_price * (1 - discount_pct / 100)
        final_price = price_after_pct - discount_amt
        return max(final_price, Decimal('0'))

    @property
    def line_total(self) -> Decimal:
        """Calculate line total with discounts applied."""
        return Decimal(str(self.quantity)) * self.effective_unit_price

    def __str__(self):
        return f"{self.product.name} x {self.quantity} @ {self.unit_price}"


class Sale(BaseSyncModel):
    """
    Point of Sale transaction header.
    Aggregates all items sold in a single transaction.
    """
    COMPLETED = "COMPLETED"
    PENDING = "PENDING"
    CANCELLED = "CANCELLED"

    STATUS_CHOICES = (
        (PENDING, "Awaiting Payment"),
        (COMPLETED, "Paid & Completed"),
        (CANCELLED, "Cancelled"),
    )

    """terminal_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        db_index=True,
        help_text="POS terminal that created this sale (browser tab UUID)"
    ) the terminal_id field is removed to simplify the model and avoid issues with multiple terminals."""

    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        related_name='sales',
        null=True,
        blank=True,
        help_text="Customer for this sale (optional for cash sales)"
    )
    sold_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='sales_processed',
        help_text="Sales person who processed transaction"
    )
    branch = models.ForeignKey(
        'Core.Branch',
        on_delete=models.PROTECT,
        related_name='sales',
        null=True,
        blank=True,
        help_text="Branch where the sale was processed"
    )
    pos_terminal = models.ForeignKey(
        'Core.POSTerminal',
        on_delete=models.SET_NULL,
        related_name='sales',
        null=True,
        blank=True,
        help_text="POS terminal that captured the sale"
    )
    reference = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
        help_text="Sales receipt / invoice reference"
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=PENDING,
        db_index=True,
        help_text="Sale completion status"
    )
    notes = models.TextField(
        blank=True,
        help_text="Sale notes (e.g., special instructions)"
    )
    # Payment tracking
    PAYMENT_METHODS = (
        ('CASH', 'Cash'),
        ('CARD', 'Card/Debit'),
        ('CHEQUE', 'Cheque'),
        ('BANK_TRANSFER', 'Bank Transfer'),
        ('LAYBY', 'Layby Completion'),
        ('OTHER', 'Other'),
    )
    payment_method = models.CharField(
        max_length=20,
        choices=PAYMENT_METHODS,
        null=True,
        blank=True,
        help_text="Payment method used"
    )
    layby = models.ForeignKey(
        'Layby',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='sales',
        help_text="Associated layby (if completing layaway)"
    )
    amount_received = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Amount received from customer (for cash payments)"
    )
    change_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Change given to customer"
    )
    inventory_sync_status = models.CharField(
        max_length=20,
        choices=[
            ('PENDING', 'Inventory sync pending'),
            ('SUCCESS', 'Inventory successfully synced'),
            ('FAILED', 'Inventory sync failed'),
        ],
        default='PENDING',
        db_index=True,
        help_text="Status of inventory deduction after completion"
    )
    inventory_completed = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Whether background inventory deduction task completed"
    )
    celery_task_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="Celery task ID for inventory completion async task"
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['reference']),
            models.Index(fields=['branch', 'created_at']),
            models.Index(fields=['customer', 'created_at']),
            models.Index(fields=['status']),
            models.Index(fields=['sold_by']),
        ]

    @property
    def total_amount(self) -> Decimal:
        """Calculate total sale amount."""
        return sum(
            Decimal(str(item.line_total))
            for item in self.items.all()
        ) or Decimal('0.00')

    @property
    def item_count(self) -> int:
        """Calculate total items in sale."""
        return sum(item.quantity for item in self.items.all())

    def clean(self):
        """Validate sale constraints."""
        # Customer is optional for walk-in/cash sales.
        # Reference format should not be used to enforce customer requirements.
        
        # Validate payment info if sale is completed
        if self.status == self.COMPLETED:
            if not self.payment_method:
                raise ValidationError("Payment method is required for completed sales")
            
            # For cash payments, validate amounts
            if self.payment_method == 'CASH':
                if self.amount_received is None:
                    raise ValidationError("Amount received is required for cash payments")
                total = self.total_amount
                if self.amount_received < total:
                    raise ValidationError(
                        f"Amount received ({self.amount_received}) must be >= total ({total})"
                    )
                # Calculate change if not already set
                if self.change_amount == 0 and self.amount_received >= total:
                    self.change_amount = self.amount_received - total

    def __str__(self):
        return f"Sale {self.reference} - {self.customer or 'Cash Sale'}"


class SaleItem(models.Model):
    client_id = models.UUIDField(unique=True, null=True, blank=True, db_index=True)
    """
    Individual line items in a sale.
    Normalized many-to-many between Sale and Product.
    """
    sale = models.ForeignKey(
        Sale,
        on_delete=models.CASCADE,
        related_name='items',
        help_text="Parent sale"
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name='sale_items',
        help_text="Product sold"
    )
    quantity = models.PositiveIntegerField(
        help_text="Quantity sold"
    )
    unit_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Selling price per unit at time of sale"
    )
    discount_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        help_text="Discount percentage (0-100%, admin only)"
    )
    discount_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Fixed discount amount per unit (admin only)"
    )
    bundle_quantity = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Bundle size snapshot applied at sale time"
    )
    bundle_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Bundle price snapshot applied at sale time"
    )

    class Meta:
        indexes = [
            models.Index(fields=['sale', 'product']),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(unit_price__gte=0),
                name='sale_unit_price_non_negative'
            ),
            models.CheckConstraint(
                condition=models.Q(discount_percentage__gte=0, discount_percentage__lte=100),
                name='sale_discount_percentage_valid'
            ),
        ]

    def clean(self):
        """Validate sale item constraints."""
        if self.quantity <= 0:
            raise ValidationError("Quantity must be greater than 0")
        if self.unit_price < 0:
            raise ValidationError("Unit price cannot be negative")
        if self.discount_percentage < 0 or self.discount_percentage > 100:
            raise ValidationError("Discount percentage must be between 0 and 100")
        if self.discount_amount < 0:
            raise ValidationError("Discount amount cannot be negative")

        has_bundle_quantity = self.bundle_quantity is not None
        has_bundle_price = self.bundle_price is not None
        if has_bundle_quantity != has_bundle_price:
            raise ValidationError("Bundle quantity and bundle price must both be set or both be empty")

        if has_bundle_quantity:
            if self.bundle_quantity <= 1:
                raise ValidationError("Bundle quantity must be greater than 1")
            if self.bundle_price < 0:
                raise ValidationError("Bundle price cannot be negative")

    @property
    def effective_unit_price(self) -> Decimal:
        """Calculate unit price after discounts."""
        base_price = Decimal(str(self.unit_price))
        discount_amt = Decimal(str(self.discount_amount))
        discount_pct = Decimal(str(self.discount_percentage))
        
        # Apply percentage discount first, then fixed amount
        price_after_pct = base_price * (1 - discount_pct / 100)
        final_price = price_after_pct - discount_amt
        return max(final_price, Decimal('0'))

    @property
    def line_total(self) -> Decimal:
        """Calculate line total with discounts applied."""
        quantity = Decimal(str(self.quantity))
        effective_price = self.effective_unit_price

        if self.bundle_quantity and self.bundle_price and self.quantity >= self.bundle_quantity:
            bundle_count = self.quantity // self.bundle_quantity
            remainder_units = self.quantity % self.bundle_quantity
            return (
                Decimal(str(bundle_count)) * Decimal(str(self.bundle_price))
                + Decimal(str(remainder_units)) * effective_price
            )

        return quantity * effective_price

    def __str__(self):
        return f"{self.product.name} x {self.quantity}"


class Layby(BaseSyncModel):
    """
    Lay-by (layaway) plan for customer installment purchases.
    Customer reserves items and pays in installments.
    """
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"

    STATUS_CHOICES = (
        (ACTIVE, "Active - Payment Pending"),
        (COMPLETED, "Fully Paid & Completed"),
        (CANCELLED, "Cancelled by Customer"),
        (REFUNDED, "Cancelled & Refunded"),
    )

    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        related_name='laybys',
        help_text="Customer holding layby"
    )
    branch = models.ForeignKey(
        'Core.Branch',
        on_delete=models.PROTECT,
        related_name='laybys',
        null=True,
        blank=True,
        help_text="Branch where the layby was opened"
    )
    pos_terminal = models.ForeignKey(
        'Core.POSTerminal',
        on_delete=models.SET_NULL,
        related_name='laybys',
        null=True,
        blank=True,
        help_text="POS terminal that captured the layby"
    )
    reference = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
        help_text="Layby agreement reference"
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=ACTIVE,
        db_index=True,
        help_text="Layby status"
    )
    total_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Total layby amount due"
    )
    due_date = models.DateField(
        help_text="Final payment due date"
    )
    notes = models.TextField(
        blank=True,
        help_text="Layby terms or notes"
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['branch', 'created_at']),
            models.Index(fields=['customer', 'status']),
            models.Index(fields=['due_date']),
            models.Index(fields=['status']),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(total_amount__gt=0),
                name='layby_total_positive'
            ),
        ]

    @property
    def total_paid(self) -> Decimal:
        """Calculate total amount paid to date."""
        return sum(
            Decimal(str(payment.amount))
            for payment in self.payments.all()
        ) or Decimal('0.00')

    @property
    def balance(self) -> Decimal:
        """Calculate remaining balance due."""
        return Decimal(str(self.total_amount)) - self.total_paid

    @property
    def is_overdue(self) -> bool:
        """Check if layby is past due."""
        return (
            timezone.now().date() > self.due_date and
            self.status == self.ACTIVE
        )

    def clean(self):
        """Validate layby constraints."""
        if self.total_amount <= 0:
            raise ValidationError("Total amount must be greater than 0")
        if self.due_date <= timezone.now().date():
            raise ValidationError("Due date must be in the future")

    def __str__(self):
        return f"Layby {self.reference} - {self.customer.name} ({self.balance} pending)"


class LaybyItem(models.Model):
    client_id = models.UUIDField(unique=True, null=True, blank=True, db_index=True)
    """
    Products held under a layby agreement.
    Normalized many-to-many between Layby and Product.
    """
    layby = models.ForeignKey(
        Layby,
        on_delete=models.CASCADE,
        related_name='items',
        help_text="Parent layby"
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name='layby_items',
        help_text="Product on layby"
    )
    quantity = models.PositiveIntegerField(
        help_text="Quantity reserved"
    )
    unit_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Price agreed for layby"
    )

    class Meta:
        unique_together = ("layby", "product")
        indexes = [
            models.Index(fields=['layby', 'product']),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(unit_price__gte=0),
                name='layby_unit_price_non_negative'
            ),
        ]

    def clean(self):
        """Validate layby item constraints."""
        if self.quantity <= 0:
            raise ValidationError("Quantity must be greater than 0")
        if self.unit_price < 0:
            raise ValidationError("Unit price cannot be negative")

    @property
    def line_total(self) -> Decimal:
        """Calculate line total."""
        return Decimal(str(self.quantity)) * Decimal(str(self.unit_price))

    def __str__(self):
        return f"{self.product.name} x {self.quantity}"


class LaybyPayment(BaseSyncModel):
    """
    Payment records for layby agreements.
    Audit trail of all customer payments.
    """
    PAYMENT_METHODS = (
        ('CASH', 'Cash'),
        ('CARD', 'Card/Debit'),
        ('CHEQUE', 'Cheque'),
        ('BANK_TRANSFER', 'Bank Transfer'),
        ('OTHER', 'Other'),
    )
    
    layby = models.ForeignKey(
        Layby,
        on_delete=models.CASCADE,
        related_name='payments',
        help_text="Associated layby"
    )
    branch = models.ForeignKey(
        'Core.Branch',
        on_delete=models.PROTECT,
        related_name='layby_payments',
        null=True,
        blank=True,
        help_text="Branch where the payment was collected"
    )
    pos_terminal = models.ForeignKey(
        'Core.POSTerminal',
        on_delete=models.SET_NULL,
        related_name='layby_payments',
        null=True,
        blank=True,
        help_text="POS terminal that captured the layby payment"
    )
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Payment amount"
    )
    reference = models.CharField(
        max_length=100,
        blank=True,
        help_text="Payment reference (cheque #, receipt #, etc.)"
    )
    payment_method = models.CharField(
        max_length=20,
        choices=PAYMENT_METHODS,
        default='CASH',
        help_text="Method payment received"
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['branch', 'created_at']),
            models.Index(fields=['layby', 'created_at']),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name='layby_payment_positive'
            ),
        ]

    def clean(self):
        """Validate payment constraints."""
        if self.amount <= 0:
            raise ValidationError("Payment must be greater than 0")
        if self.amount > self.layby.balance:
            raise ValidationError(
                f"Payment exceeds remaining balance ({self.layby.balance})"
            )

    def __str__(self):
        return f"Payment {self.amount} for {self.layby.reference}"


class LaybyRefund(BaseModel):
    """
    Refund records for cancelled laybys.
    Audit trail of refund calculations and penalties.
    """
    layby = models.OneToOneField(
        Layby,
        on_delete=models.PROTECT,
        related_name='refund',
        help_text="Associated layby being refunded"
    )
    amount_paid = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Amount customer paid"
    )
    refund_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Amount to be refunded"
    )
    penalty_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Cancellation penalty (if any)"
    )
    reason = models.TextField(
        blank=True,
        help_text="Reason for cancellation/refund"
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['layby']),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_paid__gte=0),
                name='layby_refund_amount_paid_non_negative'
            ),
            models.CheckConstraint(
                condition=models.Q(refund_amount__gte=0),
                name='layby_refund_amount_non_negative'
            ),
            models.CheckConstraint(
                condition=models.Q(penalty_amount__gte=0),
                name='layby_penalty_non_negative'
            ),
        ]

    def clean(self):
        """Validate refund constraints."""
        if self.refund_amount < 0:
            raise ValidationError("Refund cannot be negative")
        if self.refund_amount > self.amount_paid:
            raise ValidationError(
                "Refund cannot exceed amount paid"
            )
        if self.penalty_amount < 0:
            raise ValidationError("Penalty cannot be negative")
        if (self.refund_amount + self.penalty_amount) > self.amount_paid:
            raise ValidationError(
                "Refund plus penalty cannot exceed amount paid"
            )

    def __str__(self):
        return f"Refund for {self.layby.reference}: {self.refund_amount}"


class DailyReconciliation(BaseModel):
    """
    Daily settlement report aggregating all payments and sales.
    Reconciles cash, cards, cheques, and transfers received.
    Required for end-of-day variance calculation.
    """
    reconciliation_date = models.DateField(
        auto_now_add=True,
        db_index=True,
        help_text="Date of reconciliation (typically current date)"
    )
    prepared_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='reconciliations_prepared',
        help_text="User who prepared this reconciliation"
    )
    verified_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reconciliations_verified',
        help_text="Manager who verified reconciliation"
    )
    
    # Regular sales by payment method
    total_cash_sales = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Total cash sales for the day"
    )
    total_card_sales = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Total card sales for the day"
    )
    
    # Layby payments by method
    total_layby_cash = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Total layby cash payments"
    )
    total_layby_card = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Total layby card payments"
    )
    total_layby_cheques = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Total layby cheques received"
    )
    total_layby_transfers = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Total layby bank transfers"
    )
    
    # Aggregates
    total_cash_in = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Total physical cash in drawer (cash sales + layby cash)"
    )
    total_electronics = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Total electronic payments (card + transfers)"
    )
    total_cheques = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Total cheques in safe"
    )
    total_expected = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Total expected (cash + electronics + cheques)"
    )
    
    # Physical reconciliation
    counted_cash = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Actually counted physical cash in drawer"
    )
    variance = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Variance = counted_cash - total_cash_in (should be ~0)"
    )
    variance_note = models.TextField(
        blank=True,
        help_text="Explanation for any variance"
    )
    
    # Status
    is_locked = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Whether reconciliation is locked for audit"
    )
    locked_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when reconciliation was locked"
    )

    class Meta:
        ordering = ['-reconciliation_date']
        indexes = [
            models.Index(fields=['reconciliation_date']),
            models.Index(fields=['is_locked']),
            models.Index(fields=['prepared_by']),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(total_cash_sales__gte=0),
                name='reconcil_cash_sales_non_negative'
            ),
            models.CheckConstraint(
                condition=models.Q(total_card_sales__gte=0),
                name='reconcil_card_sales_non_negative'
            ),
            models.CheckConstraint(
                condition=models.Q(counted_cash__gte=0),
                name='reconcil_counted_cash_non_negative'
            ),
        ]

    @property
    def total_sales(self) -> Decimal:
        """Total sales amount (all payment methods)."""
        return (
            Decimal(str(self.total_cash_sales)) +
            Decimal(str(self.total_card_sales))
        )

    @property
    def total_layby_payments(self) -> Decimal:
        """Total layby payments (all methods)."""
        return (
            Decimal(str(self.total_layby_cash)) +
            Decimal(str(self.total_layby_card)) +
            Decimal(str(self.total_layby_cheques)) +
            Decimal(str(self.total_layby_transfers))
        )

    def clean(self):
        """Validate reconciliation constraints."""
        if self.counted_cash is not None and self.total_cash_in is not None:
            self.variance = Decimal(str(self.counted_cash)) - Decimal(str(self.total_cash_in))

    def lock_reconciliation(self, user=None):
        """Lock reconciliation for audit trail."""
        self.is_locked = True
        self.locked_at = timezone.now()
        if user:
            self.verified_by = user
        self.updated_by = user
        self.save()

    def __str__(self):
        return f"Reconciliation {self.reconciliation_date} - {self.total_expected} total"


class Return(BaseModel):
    """
    Sales and purchase returns tracking.
    Manages product returns for both customer returns and supplier returns.
    """
    RETURN_TYPES = (
        ('SALES', 'Sales Return (from customer)'),
        ('PURCHASE', 'Purchase Return (to supplier)'),
    )
    
    STATUS_CHOICES = (
        ('PENDING', 'Pending Review'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
        ('COMPLETED', 'Completed'),
    )
    
    return_number = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
        help_text="Unique return reference number"
    )
    return_type = models.CharField(
        max_length=20,
        choices=RETURN_TYPES,
        help_text="Whether this is a sales or purchase return"
    )
    
    # Reference to original transaction
    sale = models.ForeignKey(
        Sale,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='returns',
        help_text="Original sale (for sales returns)"
    )
    purchase = models.ForeignKey(
        'Inventory.Purchase',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='returns',
        help_text="Original purchase (for purchase returns)"
    )
    branch = models.ForeignKey(
        'Core.Branch',
        on_delete=models.PROTECT,
        related_name='returns',
        null=True,
        blank=True,
        help_text="Branch where the return was created"
    )
    pos_terminal = models.ForeignKey(
        'Core.POSTerminal',
        on_delete=models.SET_NULL,
        related_name='returns',
        null=True,
        blank=True,
        help_text="POS terminal that captured the return"
    )
    
    # Customer/Supplier reference
    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='returns',
        help_text="Customer for sales returns"
    )
    supplier = models.ForeignKey(
        'Inventory.Supplier',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='returns',
        help_text="Supplier for purchase returns"
    )
    
    # Return details
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='PENDING',
        db_index=True,
        help_text="Current return status"
    )
    reason = models.TextField(
        help_text="Reason for return (e.g., defective, damaged, wrong item)"
    )
    notes = models.TextField(
        blank=True,
        help_text="Additional notes about the return"
    )
    
    # Financial tracking
    total_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Total value of returned items"
    )
    refund_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Amount refunded to customer (for sales returns)"
    )
    refund_method = models.CharField(
        max_length=20,
        choices=[
            ('CASH', 'Cash'),
            ('CARD', 'Card/Debit'),
            ('CREDIT', 'Account Credit'),
            ('PENDING', 'Pending'),
        ],
        default='PENDING',
        help_text="How refund was/will be processed"
    )
    
    # Approval workflow
    approved_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_returns',
        help_text="User who approved this return"
    )
    approved_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When return was approved"
    )
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['branch', 'created_at']),
            models.Index(fields=['return_number']),
            models.Index(fields=['return_type', 'status']),
            models.Index(fields=['customer', 'created_at']),
            models.Index(fields=['supplier', 'created_at']),
            models.Index(fields=['created_at']),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(total_amount__gte=0),
                name='return_amount_non_negative'
            ),
            models.CheckConstraint(
                condition=models.Q(refund_amount__gte=0),
                name='return_refund_non_negative'
            ),
        ]
    
    def clean(self):
        """Validate return constraints."""
        # Check that either sale or purchase is provided (but not both)
        if self.return_type == 'SALES' and not self.sale:
            raise ValidationError("Sales returns must reference a sale")
        if self.return_type == 'PURCHASE' and not self.purchase:
            raise ValidationError("Purchase returns must reference a purchase")
        
        # Check that customer/supplier matches return type
        if self.return_type == 'SALES' and not self.customer:
            raise ValidationError("Sales returns must have a customer")
        if self.return_type == 'PURCHASE' and not self.supplier:
            raise ValidationError("Purchase returns must have a supplier")
    
    @classmethod
    def create_from_sale(cls, sale, customer, reason, items_data, user, notes=''):
        """
        Create a sales return directly from a POS sale.
        
        Args:
            sale: Sale instance to return
            customer: Customer returning items
            reason: Reason for return
            items_data: List of dicts with product_id, quantity, unit_price
            user: User creating the return
            notes: Optional additional notes
            
        Returns:
            Return: Created return instance
            
        Raises:
            ValidationError: If data is invalid
        """
        from django.db import transaction
        
        if sale.status != 'COMPLETED':
            raise ValidationError(f"Cannot return incomplete sale (status: {sale.status})")
        
        with transaction.atomic():
            # Calculate total amount
            total_amount = sum(
                Decimal(str(item['unit_price'])) * item['quantity']
                for item in items_data
            )
            
            # Create return
            return_obj = cls.objects.create(
                return_type='SALES',
                sale=sale,
                customer=customer,
                reason=reason,
                notes=notes,
                total_amount=total_amount,
                created_by=user,
                updated_by=user
            )
            
            # Create return items
            from .models import ReturnItem
            for item_data in items_data:
                ReturnItem.objects.create(
                    return_record=return_obj,
                    product_id=item_data['product_id'],
                    quantity=item_data['quantity'],
                    unit_price=item_data['unit_price'],
                    reason=item_data.get('reason', 'Customer return')
                )
            
            return return_obj
    
    def approve(self, user=None):
        """Approve the return (deprecated - use ReturnProcessor instead)."""
        self.status = 'APPROVED'
        self.approved_by = user
        self.approved_at = timezone.now()
        self.updated_by = user
        self.save()
    
    def reject(self):
        """Reject the return (deprecated - use ReturnProcessor instead)."""
        self.status = 'REJECTED'
        self.save()
    
    def complete(self, user=None):
        """Mark return as completed (deprecated - use ReturnProcessor instead)."""
        self.status = 'COMPLETED'
        self.updated_by = user
        self.save()
    
    @property
    def item_count(self) -> int:
        """Count of items being returned."""
        return sum(item.quantity for item in self.items.all())
    
    def __str__(self):
        return f"Return {self.return_number} ({self.get_return_type_display()})"


class ReturnItem(models.Model):
    """
    Individual items in a return.
    Tracks what products are being returned and quantities.
    """
    return_record = models.ForeignKey(
        Return,
        on_delete=models.CASCADE,
        related_name='items',
        help_text="Parent return"
    )
    
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name='returned_items',
        help_text="Product being returned"
    )
    
    quantity = models.PositiveIntegerField(
        help_text="Quantity being returned"
    )
    
    unit_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Unit price at time of original transaction"
    )
    
    reason = models.CharField(
        max_length=100,
        help_text="Reason for returning this item (e.g., defective, damaged)"
    )
    
    class Meta:
        indexes = [
            models.Index(fields=['return_record', 'product']),
        ]
    
    @property
    def line_total(self) -> Decimal:
        """Calculate line total."""
        return Decimal(str(self.quantity)) * Decimal(str(self.unit_price))
    
    def __str__(self):
        return f"Return Item: {self.product.name} x{self.quantity}"


# Signal handlers for auto-generating reference numbers
from django.db.models.signals import pre_save
from django.dispatch import receiver


@receiver(pre_save, sender=Return)
def generate_return_number(sender, instance, **kwargs):
    """Auto-generate return number if not provided."""
    if not instance.return_number:
        timestamp = timezone.now().strftime('%Y%m%d%H%M%S')
        type_prefix = 'SR' if instance.return_type == 'SALES' else 'PR'
        count = Return.objects.count() + 1
        instance.return_number = f"{type_prefix}-{timestamp}-{count:04d}"


@receiver(pre_save, sender=Quotation)
def generate_quotation_reference(sender, instance, **kwargs):
    """Auto-generate quotation reference if not provided."""
    if not instance.reference:
        timestamp = timezone.now().strftime('%Y%m%d%H%M%S')
        count = Quotation.objects.count() + 1
        instance.reference = f"QT-{timestamp}-{count:04d}"
