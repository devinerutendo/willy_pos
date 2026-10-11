from django.db import models
from django.core.exceptions import ValidationError
from Core.models import BaseModel, BaseSyncModel
def generate_sku(category: "Category") -> str:
    """
    Generate the next globally-unique SKU for a given category.
 
    Format: <CAT>-<NNNN>
      - <CAT>  : first three letters of the category name, uppercased.
      - <NNNN> : zero-padded 4-digit sequence derived from the highest
                 existing SKU sequence number across *all* products,
                 ensuring global uniqueness regardless of category.
 
    The query is wrapped in select_for_update() so concurrent product
    creation cannot allocate the same sequence number.
 
    Raises:
        ValueError: When the 4-digit sequence space (0000-9999) is
                    exhausted — i.e., 10 000 products exist.
    """
    prefix = category.name[:3].upper()
 
    # Lock all existing SKU rows for the duration of this transaction so
    # no two concurrent saves can read the same "max" and collide.
    last_product = (
        Product.objects.select_for_update()
        .filter(sku__isnull=False)
        .order_by("-sku_sequence")
        .first()
    )
 
    next_sequence = (last_product.sku_sequence + 1) if last_product else 0
 
    if next_sequence > 9999:
        raise ValueError(
            "SKU sequence space exhausted (0000–9999). "
            "Extend the sequence length before adding more products."
        )
 
    return f"{prefix}-{next_sequence:04d}"


class Category(BaseSyncModel):
    """
    Product categories for organizational structure.
    Inherits audit trail from BaseModel.
    """
    name = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
        help_text="Unique category name"
    )
    description = models.TextField(
        blank=True,
        help_text="Category description"
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Whether category is available for use"
    )

    class Meta:
        ordering = ['name']
        verbose_name_plural = "Categories"
        indexes = [
            models.Index(fields=['name', 'is_active']),
        ]

    def __str__(self):
        return self.name


class Product(BaseSyncModel):
    """
    Core product entity with integrated pricing.
    Pricing is set per product (not per variant) following First Normal Form.

    SKU Assignment
    --------------
    SKUs are system-generated on first save and never overwritten.
    Format: <CAT>-<NNNN>  (e.g. ELE-0001, CLO-0042).
    The four-digit suffix is a globally-ascending sequence shared across
    all products, which guarantees uniqueness without any per-category
    counter table.
 
    Do not supply a value for `sku` manually — it is derived automatically
    from the linked category the moment the product is first persisted.
    `sku_sequence` is an internal bookkeeping field; treat it as read-only.
    """
    name = models.CharField(
        max_length=255,
        db_index=True,
        help_text="Product name"
    )
    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        related_name='products',
        help_text="Product category"
    )

    # SKU — managed entirely by the model; never set by callers directly.
    sku = models.CharField(
        max_length=10,          # "XXX-9999" = 8 chars; a little headroom
        unique=True,
        db_index=True,
        editable=False,         # hidden in Django admin / DRF by default
        help_text="Auto-assigned Stock Keeping Unit (category prefix + 4-digit sequence)",
    )
    sku_sequence = models.PositiveIntegerField(
        db_index=True,
        editable=False,
        help_text="Internal numeric component of the SKU; used for ordering during generation",
    )

    barcode = models.CharField(
        max_length=50,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
        help_text="Barcode for scanning"
    )
    description = models.TextField(
        blank=True,
        help_text="Detailed product description"
    )
    
    # Pricing fields - enforced as decimal for financial accuracy
    cost_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Cost price for procurement"
    )
    selling_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Retail selling price"
    )
    bundle_quantity = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Minimum quantity that qualifies for bundle pricing"
    )
    bundle_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Total price for one bundle"
    )
    
    reorder_level = models.PositiveIntegerField(
        default=5,
        help_text="Minimum quantity before reorder alert"
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Whether product is available for sale"
    )

    class Meta:
        ordering = ['name']
        indexes = [
            models.Index(fields=['sku']),
            models.Index(fields=["sku_sequence"]),
            models.Index(fields=['category', 'is_active']),
            models.Index(fields=['name']),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(cost_price__gte=0),
                name='cost_price_non_negative'
            ),
            models.CheckConstraint(
                condition=models.Q(selling_price__gte=0),
                name='selling_price_non_negative'
            ),
            models.CheckConstraint(
                condition=models.Q(bundle_price__isnull=True) | models.Q(bundle_price__gte=0),
                name='bundle_price_non_negative_or_null'
            ),
        ]
    
     # SKU lifecycle
    def _assign_sku(self) -> None:
        """
        Compute and lock in the SKU for a brand-new product.
 
        Called exactly once — during the first save() — inside the same
        database transaction, so generate_sku()'s select_for_update()
        prevents races between concurrent requests.
        """
        from django.db import transaction
 
        with transaction.atomic():
            sku = generate_sku(self.category)
            sequence = int(sku.split("-")[1])
            self.sku = sku
            self.sku_sequence = sequence
 
    def save(self, *args, **kwargs) -> None:
        """
        Intercept save to auto-assign the SKU on first creation.
 
        Re-assigning the SKU on updates is explicitly blocked: changing
        a product's category after creation should *not* silently alter
        its SKU, as that would break barcodes, purchase orders, and any
        external reference already in the field.
        """
        if not self.pk:
            # Brand-new product — generate SKU before the INSERT.
            self._assign_sku()
 
        super().save(*args, **kwargs)

    # Validation
    def clean(self):
        """Validate business logic constraints."""
        if self.cost_price < 0:
            raise ValidationError("Cost price cannot be negative")
        if self.selling_price < 0:
            raise ValidationError("Selling price cannot be negative")
        if self.selling_price < self.cost_price:
            raise ValidationError(
                "Selling price must be greater than or equal to cost price"
            )

        has_bundle_quantity = self.bundle_quantity is not None
        has_bundle_price = self.bundle_price is not None

        if has_bundle_quantity != has_bundle_price:
            raise ValidationError(
                "Bundle quantity and bundle price must both be provided together"
            )

        if has_bundle_quantity:
            if self.bundle_quantity <= 1:
                raise ValidationError("Bundle quantity must be greater than 1")
            if self.bundle_price < 0:
                raise ValidationError("Bundle price cannot be negative")

    @property
    def profit_margin(self) -> float:
        """Calculate profit margin percentage."""
        if self.cost_price == 0:
            return 0
        return ((self.selling_price - self.cost_price) / self.cost_price) * 100

    def __str__(self):
        return self.name


class Location(BaseSyncModel):
    """
    Physical storage locations (warehouses, shelves, etc.).
    Each location can hold inventory for any product.
    """
    branch = models.ForeignKey(
        'Core.Branch',
        on_delete=models.PROTECT,
        related_name='locations',
        null=True,
        blank=True,
        help_text="Branch that owns this storage location"
    )
    name = models.CharField(
        max_length=100,
        db_index=True,
        help_text="Location identifier (e.g., Warehouse A, Shelf B1)"
    )
    address = models.TextField(
        blank=True,
        help_text="Full address of the location"
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Whether location is currently in use"
    )

    class Meta:
        ordering = ['name']
        indexes = [
            models.Index(fields=['branch', 'name']),
            models.Index(fields=['name', 'is_active']),
        ]
        constraints = [
            models.UniqueConstraint(fields=['branch', 'name'], name='unique_location_name_per_branch'),
        ]

    def __str__(self):
        if self.branch:
            return f"{self.branch.code} - {self.name}"
        return self.name


class Stock(models.Model):
    client_id = models.UUIDField(unique=True, null=True, blank=True, db_index=True)
    """
    Inventory levels per product per location.
    Normalized to track physical inventory in real-time.
    """
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name='stock_levels',
        help_text="Product in inventory"
    )
    location = models.ForeignKey(
        Location,
        on_delete=models.CASCADE,
        related_name='stock_levels',
        help_text="Storage location"
    )
    quantity = models.IntegerField(
        default=0,
        help_text="Current physical stock quantity"
    )
    reserved_quantity = models.IntegerField(
        default=0,
        help_text="Quantity reserved for pending sales"
    )

    class Meta:
        unique_together = ("product", "location")
        indexes = [
            models.Index(fields=['product', 'location']),
            models.Index(fields=['quantity']),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gte=0),
                name='quantity_non_negative'
            ),
            models.CheckConstraint(
                condition=models.Q(reserved_quantity__gte=0),
                name='reserved_quantity_non_negative'
            ),
        ]

    @property
    def available_stock(self) -> int:
        """Calculate available stock (not reserved)."""
        return self.quantity - self.reserved_quantity

    def clean(self):
        """Validate stock constraints."""
        if self.quantity < 0:
            raise ValidationError("Stock quantity cannot be negative")
        if self.reserved_quantity < 0:
            raise ValidationError("Reserved quantity cannot be negative")
        if self.reserved_quantity > self.quantity:
            raise ValidationError(
                "Reserved quantity cannot exceed total quantity"
            )

    def __str__(self):
        return f"{self.product.name} @ {self.location.name} ({self.available_stock} available)"


class StockTransaction(BaseSyncModel):
    """
    Audit trail for all stock movements.
    Required for compliance and inventory reconciliation.
    """
    IN = "IN"
    OUT = "OUT"
    RETURN = "RETURN"
    ADJUSTMENT = "ADJUSTMENT"

    TRANSACTION_TYPES = (
        (IN, "Stock In - Purchase Receipt"),
        (OUT, "Stock Out - Sale"),
        (RETURN, "Return - Customer/Supplier"),
        (ADJUSTMENT, "Adjustment - Inventory Count"),
    )

    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name='transactions',
        help_text="Product involved in transaction"
    )
    location = models.ForeignKey(
        Location,
        on_delete=models.PROTECT,
        related_name='transactions',
        help_text="Location where transaction occurred"
    )
    branch = models.ForeignKey(
        'Core.Branch',
        on_delete=models.PROTECT,
        related_name='stock_transactions',
        null=True,
        blank=True,
        help_text="Branch responsible for this movement"
    )
    pos_terminal = models.ForeignKey(
        'Core.POSTerminal',
        on_delete=models.SET_NULL,
        related_name='stock_transactions',
        null=True,
        blank=True,
        help_text="POS terminal that originated the movement when applicable"
    )
    transaction_type = models.CharField(
        max_length=20,
        choices=TRANSACTION_TYPES,
        db_index=True,
        help_text="Type of stock movement"
    )
    quantity = models.IntegerField(
        help_text="Quantity moved (sign indicates direction)"
    )
    reference = models.CharField(
        max_length=100,
        db_index=True,
        help_text="External reference (PO #, SO #, Invoice #, etc.)"
    )
    notes = models.TextField(
        blank=True,
        help_text="Additional transaction notes"
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['product', 'created_at']),
            models.Index(fields=['transaction_type', 'created_at']),
            models.Index(fields=['reference']),
        ]

    def clean(self):
        """Validate transaction constraints."""
        if self.quantity == 0:
            raise ValidationError("Transaction quantity cannot be zero")

    def __str__(self):
        return f"{self.get_transaction_type_display()} - {self.product.name} ({self.quantity})"


class StockAdjustment(BaseSyncModel):
    """Formal stock adjustment record capturing before/after quantities."""

    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name='stock_adjustments',
        help_text="Product being adjusted"
    )
    location = models.ForeignKey(
        Location,
        on_delete=models.PROTECT,
        related_name='stock_adjustments',
        help_text="Location where the count was corrected"
    )
    branch = models.ForeignKey(
        'Core.Branch',
        on_delete=models.PROTECT,
        related_name='stock_adjustments',
        null=True,
        blank=True,
        help_text="Branch responsible for the adjustment"
    )
    pos_terminal = models.ForeignKey(
        'Core.POSTerminal',
        on_delete=models.SET_NULL,
        related_name='stock_adjustments',
        null=True,
        blank=True,
        help_text="POS terminal involved when the count was adjusted from a terminal"
    )
    quantity_before = models.IntegerField(help_text="Quantity before adjustment")
    quantity_after = models.IntegerField(help_text="Quantity after adjustment")
    quantity_delta = models.IntegerField(help_text="Net quantity change applied")
    reason = models.CharField(max_length=255, help_text="Business reason for the adjustment")
    reference = models.CharField(max_length=100, blank=True, db_index=True, help_text="Adjustment ticket or reference")
    notes = models.TextField(blank=True, help_text="Additional notes for the adjustment")

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['branch', 'created_at']),
            models.Index(fields=['product', 'created_at']),
            models.Index(fields=['reference']),
        ]

    def clean(self):
        if self.quantity_after < 0:
            raise ValidationError("Adjusted quantity cannot be negative")
        if self.quantity_delta == 0:
            raise ValidationError("Adjustment must change the stock quantity")

    @classmethod
    def apply_adjustment(
        cls,
        *,
        stock,
        new_quantity,
        user,
        reason,
        reference='',
        notes='',
        pos_terminal=None,
    ):
        if new_quantity < 0:
            raise ValidationError("Adjusted quantity cannot be negative")

        quantity_before = stock.quantity
        quantity_delta = new_quantity - quantity_before
        if quantity_delta == 0:
            raise ValidationError("Adjustment must change the stock quantity")

        adjustment = cls.objects.create(
            product=stock.product,
            location=stock.location,
            branch=stock.location.branch,
            pos_terminal=pos_terminal,
            quantity_before=quantity_before,
            quantity_after=new_quantity,
            quantity_delta=quantity_delta,
            reason=reason,
            reference=reference,
            notes=notes,
            created_by=user,
            updated_by=user,
        )

        stock.quantity = new_quantity
        stock.save(update_fields=['quantity'])

        StockTransaction.objects.create(
            product=stock.product,
            location=stock.location,
            branch=stock.location.branch,
            pos_terminal=pos_terminal,
            transaction_type=StockTransaction.ADJUSTMENT,
            quantity=quantity_delta,
            reference=reference or f"ADJ-{adjustment.id}",
            notes=reason if not notes else f"{reason}\n{notes}",
            created_by=user,
            updated_by=user,
        )

        return adjustment

    def __str__(self):
        return f"Adjustment {self.reference or self.id} - {self.product.name} ({self.quantity_delta:+d})"


class Supplier(BaseSyncModel):
    """
    Vendor information for procurement operations.
    """
    name = models.CharField(
        max_length=255,
        db_index=True,
        help_text="Supplier company name"
    )
    email = models.EmailField(
        help_text="Primary contact email"
    )
    phone = models.CharField(
        max_length=20,
        help_text="Contact phone number"
    )
    address = models.TextField(
        help_text="Full supplier address"
    )
    contact_person = models.CharField(
        max_length=255,
        blank=True,
        help_text="Primary contact person name"
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Whether supplier is actively used"
    )

    class Meta:
        ordering = ['name']
        indexes = [
            models.Index(fields=['name', 'is_active']),
            models.Index(fields=['email']),
        ]

    def __str__(self):
        return self.name


class Purchase(BaseSyncModel):
    """
    Purchase order header - aggregates supplier deliveries.
    Each purchase represents an invoice/delivery from a supplier.
    """
    PENDING = "PENDING"
    RECEIVED = "RECEIVED"
    INVOICED = "INVOICED"
    CANCELLED = "CANCELLED"

    STATUS_CHOICES = (
        (PENDING, "Pending Receipt"),
        (RECEIVED, "Partially Received"),
        (INVOICED, "Fully Received & Invoiced"),
        (CANCELLED, "Cancelled"),
    )

    branch = models.ForeignKey(
        'Core.Branch',
        on_delete=models.PROTECT,
        related_name='purchases',
        null=True,
        blank=True,
        help_text="Branch receiving this purchase"
    )
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        related_name='purchases',
        help_text="Source supplier"
    )
    reference = models.CharField(
        max_length=100,
        unique=True,
        blank=True,
        db_index=True,
        help_text="Purchase order / Invoice reference"
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=PENDING,
        db_index=True,
        help_text="Current purchase status"
    )
    delivery_date = models.DateField(
        null=True,
        blank=True,
        help_text="Expected or actual delivery date"
    )
    notes = models.TextField(
        blank=True,
        help_text="Purchase notes or special conditions"
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['supplier', 'created_at']),
            models.Index(fields=['status']),
            models.Index(fields=['reference']),
        ]

    @property
    def total_cost(self) -> float:
        """Calculate total purchase cost."""
        return sum(
            float(item.quantity * item.cost_price)
            for item in self.items.all()
        )

    def update_stock(self, user=None):
        """
        Update product stock quantities based on received items.
        Gets or creates default storage location and increments stock.
        """
        from django.db.models import F
        
        # Get or create default location for received inventory
        location, _ = Location.objects.get_or_create(
            branch=self.branch,
            name="Main Warehouse",
            defaults={"is_active": True, "created_by": user, "updated_by": user, "branch": self.branch}
        )
        
        items = self.items.all()
        for item in items:
            received_qty = item.received_quantity or item.quantity
            
            if received_qty > 0:
                # Get or create stock record
                stock, created = Stock.objects.get_or_create(
                    product=item.product,
                    location=location,
                    defaults={"quantity": 0, "reserved_quantity": 0}
                )
                
                # Increment stock quantity
                stock.quantity = F('quantity') + received_qty
                stock.save()
                stock.refresh_from_db()
                
                # Create transaction record for audit trail
                StockTransaction.objects.create(
                    product=item.product,
                    location=location,
                    branch=self.branch,
                    transaction_type=StockTransaction.IN,
                    quantity=received_qty,
                    reference=self.reference,
                    notes=f"Purchase from {self.supplier.name}",
                    created_by=user,
                    updated_by=user
                )

    def __str__(self):
        return f"{self.reference} - {self.supplier.name}"


class PurchaseItem(models.Model):
    client_id = models.UUIDField(unique=True, null=True, blank=True, db_index=True)
    """
    Line items for a purchase order.
    Normalized many-to-many between Purchase and Product.
    """
    purchase = models.ForeignKey(
        Purchase,
        on_delete=models.CASCADE,
        related_name='items',
        help_text="Parent purchase order"
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name='purchase_items',
        help_text="Product being purchased"
    )
    quantity = models.PositiveIntegerField(
        help_text="Quantity ordered"
    )
    cost_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Unit cost from supplier"
    )
    received_quantity = models.PositiveIntegerField(
        default=0,
        help_text="Quantity received to date"
    )

    class Meta:
        unique_together = ("purchase", "product")
        indexes = [
            models.Index(fields=['purchase', 'product']),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(cost_price__gte=0),
                name='purchase_cost_non_negative'
            ),
        ]

    def clean(self):
        """Validate purchase item constraints."""
        if self.quantity <= 0:
            raise ValidationError("Quantity must be greater than 0")
        if self.cost_price < 0:
            raise ValidationError("Cost price cannot be negative")
        if self.received_quantity > self.quantity:
            raise ValidationError(
                "Received quantity cannot exceed ordered quantity"
            )

    @property
    def line_total(self) -> float:
        """Calculate line total cost."""
        return float(self.quantity * self.cost_price)

    @property
    def remaining_quantity(self) -> int:
        """Calculate quantity still pending receipt."""
        return self.quantity - self.received_quantity

    def __str__(self):
        return f"{self.product.name} x {self.quantity} @ {self.cost_price}"


class StockMovement(BaseSyncModel):
    MOVEMENT_TYPES = [
        ('SALE', 'Sale Deduct'),
        ('RETURN', 'Customer Return'),
        ('PURCHASE', 'Purchase Receipt'),
        ('ADJUSTMENT', 'Manual Count Adjustment'),
    ]

    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='movements')
    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name='movements')
    branch = models.ForeignKey('Core.Branch', on_delete=models.PROTECT, related_name='movements', null=True, blank=True)
    quantity_before = models.IntegerField()
    quantity_delta = models.IntegerField()
    quantity_after = models.IntegerField()
    movement_type = models.CharField(max_length=20, choices=MOVEMENT_TYPES)
    reference_type = models.CharField(max_length=50)  # e.g., 'Sale', 'Purchase', 'StockAdjustment'
    reference_id = models.CharField(max_length=100)    # Stores database ID or client_id

    class Meta:
        ordering = ['-created_at']
