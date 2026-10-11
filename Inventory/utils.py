"""
Inventory utilities for purchase reference generation, stock management, etc.
"""
from datetime import datetime
from django.db import transaction
from .models import Purchase


def generate_purchase_reference():
    """
    Generate a professional date-based purchase reference.
    Format: PO-YYYY-MM-[sequential number]
    Example: PO-2025-03-001, PO-2025-03-002, etc.
    
    Thread-safe and handles concurrent requests.
    """
    today = datetime.now().date()
    year = today.year
    month = today.month
    
    # Format: PO-2025-03
    reference_prefix = f"PO-{year}-{month:02d}"
    
    with transaction.atomic():
        # Find the highest number for today's date
        today_purchases = Purchase.objects.filter(
            reference__startswith=reference_prefix
        ).order_by('-reference')
        
        if today_purchases.exists():
            # Extract the last number and increment
            last_ref = today_purchases.first().reference
            try:
                last_num = int(last_ref.split('-')[-1])
                next_num = last_num + 1
            except (ValueError, IndexError):
                next_num = 1
        else:
            next_num = 1
        
        # Generate the reference
        new_reference = f"{reference_prefix}-{next_num:03d}"
        
        # Verify it doesn't exist (shouldn't happen, but safety check)
        while Purchase.objects.filter(reference=new_reference).exists():
            next_num += 1
            new_reference = f"{reference_prefix}-{next_num:03d}"
        
        return new_reference
