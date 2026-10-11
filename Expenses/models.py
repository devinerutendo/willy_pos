from django.db import models
from django.core.exceptions import ValidationError
from django.utils import timezone

from Core.models import BaseModel
# ---------------------------------------------------------------------------
# Expense Category
# ---------------------------------------------------------------------------

class ExpenseCategory(BaseModel):
    """
    Classification labels for expenses.

    Keeping categories in their own table lets managers add new types
    (Fuel, Office Supplies, Travel, etc.) without a code change or
    migration. Categories are soft-deleted via is_active so historical
    expenses that reference a retired category are never orphaned.
    """

    name = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
        help_text="Expense category name (e.g. Rent, Utilities, Salaries)",
    )
    description = models.TextField(
        blank=True,
        help_text="Optional description of what belongs in this category",
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Inactive categories are hidden from entry forms but retained for history",
    )

    class Meta:
        ordering = ["name"]
        verbose_name = "Expense Category"
        verbose_name_plural = "Expense Categories"
        indexes = [
            models.Index(fields=["name", "is_active"]),
        ]

    def __str__(self):
        return self.name


# ---------------------------------------------------------------------------
# Expense
# ---------------------------------------------------------------------------

class Expense(BaseModel):
    """
    A single business expense incurred by a staff member.

    Design notes
    ------------
    - ``incurred_by`` records whose pocket the money came out of; it
      defaults to the creating user but can be overridden by a manager
      entering an expense on someone else's behalf.
    - ``expense_date`` is explicitly stored (not derived from
      ``created_at``) because expenses are often entered after the fact.
    - All monetary values use DecimalField to avoid floating-point drift
      on financial aggregates.
    - Status follows a simple PENDING → APPROVED / REJECTED lifecycle so
      managers can review submissions without a heavyweight workflow.
    """

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"

    STATUS_CHOICES = (
        (PENDING, "Pending Review"),
        (APPROVED, "Approved"),
        (REJECTED, "Rejected"),
    )

    # ------------------------------------------------------------------ #
    # Core fields                                                          #
    # ------------------------------------------------------------------ #

    category = models.ForeignKey(
        ExpenseCategory,
        on_delete=models.PROTECT,
        related_name="expenses",
        help_text="Expense category",
    )
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Amount spent",
    )
    expense_date = models.DateField(
        default=timezone.now,
        db_index=True,
        help_text="Date the expense was incurred",
    )
    description = models.TextField(
        help_text="What the expense was for",
    )

    # ------------------------------------------------------------------ #
    # Ownership                                                            #
    # ------------------------------------------------------------------ #

    incurred_by = models.ForeignKey(
        "Accounts.User",
        on_delete=models.PROTECT,
        related_name="expenses_incurred",
        help_text="Staff member who incurred the expense",
    )

    # ------------------------------------------------------------------ #
    # Review lifecycle                                                     #
    # ------------------------------------------------------------------ #

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=PENDING,
        db_index=True,
        help_text="Approval status",
    )
    reviewed_by = models.ForeignKey(
        "Accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="expenses_reviewed",
        help_text="Manager who approved or rejected the expense",
    )
    reviewed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp of the approval or rejection decision",
    )
    review_note = models.TextField(
        blank=True,
        help_text="Optional note from the reviewer (e.g. reason for rejection)",
    )

    class Meta:
        ordering = ["-expense_date", "-created_at"]
        verbose_name = "Expense"
        verbose_name_plural = "Expenses"
        indexes = [
            models.Index(fields=["expense_date"]),
            models.Index(fields=["status"]),
            models.Index(fields=["incurred_by", "expense_date"]),
            models.Index(fields=["category", "expense_date"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="expense_amount_positive",
            ),
        ]

    # ------------------------------------------------------------------ #
    # Validation                                                           #
    # ------------------------------------------------------------------ #

    def clean(self) -> None:
        """Enforce business-logic constraints."""
        super().clean()

        if self.amount is not None and self.amount <= 0:
            raise ValidationError("Expense amount must be greater than zero")

        if self.expense_date and self.expense_date > timezone.now().date():
            raise ValidationError("Expense date cannot be in the future")

        # Reviewed fields must move together — partial state is invalid.
        has_reviewer = bool(self.reviewed_by_id)
        has_timestamp = bool(self.reviewed_at)
        if has_reviewer != has_timestamp:
            raise ValidationError(
                "reviewed_by and reviewed_at must both be set when recording a review decision"
            )

        if self.status in (self.APPROVED, self.REJECTED) and not self.reviewed_by_id:
            raise ValidationError(
                f"A reviewer must be recorded when marking an expense as {self.status}"
            )

    # ------------------------------------------------------------------ #
    # Review actions                                                       #
    # ------------------------------------------------------------------ #

    def approve(self, reviewer: "Accounts.User", note: str = "") -> None:
        """
        Approve this expense.

        Args:
            reviewer: The manager approving the expense.
            note:     Optional approval note.

        Raises:
            ValidationError: If the expense is not in PENDING status.
        """
        if self.status != self.PENDING:
            raise ValidationError(
                f"Only pending expenses can be approved (current status: {self.status})"
            )

        self.status = self.APPROVED
        self.reviewed_by = reviewer
        self.reviewed_at = timezone.now()
        self.review_note = note
        self.updated_by = reviewer
        self.save()

    def reject(self, reviewer: "Accounts.User", note: str = "") -> None:
        """
        Reject this expense.

        Args:
            reviewer: The manager rejecting the expense.
            note:     Reason for rejection (strongly encouraged).

        Raises:
            ValidationError: If the expense is not in PENDING status.
        """
        if self.status != self.PENDING:
            raise ValidationError(
                f"Only pending expenses can be rejected (current status: {self.status})"
            )

        self.status = self.REJECTED
        self.reviewed_by = reviewer
        self.reviewed_at = timezone.now()
        self.review_note = note
        self.updated_by = reviewer
        self.save()

    # ------------------------------------------------------------------ #
    # Properties                                                           #
    # ------------------------------------------------------------------ #

    @property
    def is_pending(self) -> bool:
        return self.status == self.PENDING

    @property
    def is_approved(self) -> bool:
        return self.status == self.APPROVED

    @property
    def is_rejected(self) -> bool:
        return self.status == self.REJECTED

    def __str__(self):
        return (
            f"{self.category.name} — {self.amount} "
            f"({self.expense_date}) [{self.get_status_display()}]"
        )