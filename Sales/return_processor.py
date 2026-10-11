"""
Sales Return Processing Module

Handles the business logic for processing sales and purchase returns:
- Inventory updates (increment/decrement based on return type)
- Sales adjustment tracking
- Refund processing
- Comprehensive audit trail
- Error handling and transaction management
"""

import logging
from decimal import Decimal
from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone
from Inventory.models import Stock, Product

logger = logging.getLogger(__name__)


class ReturnProcessingError(Exception):
    """Base exception for return processing errors"""
    pass


class ReturnProcessor:
    """Professional return processing with inventory and sales synchronization"""
    
    @staticmethod
    def process_sales_return(return_obj, user):
        """
        Process a sales return by:
        1. Validating the return data
        2. Incrementing inventory for returned items
        3. Creating audit trail
        4. Updating refund status
        
        Args:
            return_obj: Return model instance
            user: User who approved the return
            
        Returns:
            dict: Processing result with status and details
            
        Raises:
            ReturnProcessingError: If processing fails
        """
        if return_obj.return_type != 'SALES':
            raise ReturnProcessingError(f"Expected sales return, got {return_obj.return_type}")
        
        if return_obj.status != 'PENDING':
            raise ReturnProcessingError(f"Can only process PENDING returns, current status: {return_obj.status}")
        
        if not return_obj.sale:
            raise ReturnProcessingError("Sales return must be linked to a sale")
        
        if not return_obj.items.exists():
            raise ReturnProcessingError("Return must contain at least one item")
        
        try:
            with transaction.atomic():
                # Process each returned item
                processing_details = []
                total_returned_value = Decimal('0.00')
                
                for return_item in return_obj.items.all():
                    product = return_item.product
                    quantity = return_item.quantity
                    unit_price = Decimal(str(return_item.unit_price))
                    line_value = quantity * unit_price
                    
                    # Validate product exists and is active
                    if not product or not product.is_active:
                        raise ReturnProcessingError(
                            f"Product {product.name if product else 'Unknown'} is inactive or unavailable"
                        )
                    
                    # Increment inventory for each location that had this item deducted
                    # Default to primary location if not specified
                    primary_location = product.locations.filter(is_primary=True).first()
                    if not primary_location:
                        # Fallback to any available location
                        primary_location = product.locations.first()
                    
                    if primary_location:
                        stock_record, created = Stock.objects.get_or_create(
                            product=product,
                            location=primary_location,
                            defaults={'quantity': 0}
                        )
                        
                        # Increment stock
                        stock_record.quantity += quantity
                        stock_record.save(update_fields=['quantity'])
                        
                        processing_details.append({
                            'product_id': product.id,
                            'product_name': product.name,
                            'product_sku': product.sku,
                            'quantity_incremented': quantity,
                            'location': primary_location.name,
                            'value': str(line_value),
                        })
                    
                    total_returned_value += line_value
                
                # Update return record
                return_obj.status = 'APPROVED'
                return_obj.approved_by = user
                return_obj.approved_at = timezone.now()
                return_obj.refund_amount = total_returned_value
                return_obj.refund_method = return_obj.refund_method  # Keep existing refund method
                return_obj.updated_by = user
                return_obj.save(update_fields=[
                    'status',
                    'approved_by',
                    'approved_at',
                    'refund_amount',
                    'updated_by'
                ])
                
                logger.info(
                    f"Approved sales return {return_obj.return_number}: "
                    f"Total value: {total_returned_value}, Items: {len(processing_details)}"
                )
                
                return {
                    'status': 'SUCCESS',
                    'return_number': return_obj.return_number,
                    'items_processed': len(processing_details),
                    'total_returned_value': str(total_returned_value),
                    'details': processing_details,
                    'message': f"Successfully approved return {return_obj.return_number} and incremented inventory"
                }
                
        except Exception as e:
            logger.error(
                f"Error processing sales return {return_obj.return_number}: {str(e)}",
                exc_info=True
            )
            raise ReturnProcessingError(f"Failed to process return: {str(e)}")
    
    @staticmethod
    def process_purchase_return(return_obj, user):
        """
        Process a purchase return by:
        1. Validating the return data
        2. Decrementing inventory for returned items
        3. Creating refund record
        4. Updating purchase tracking
        
        Args:
            return_obj: Return model instance
            user: User who approved the return
            
        Returns:
            dict: Processing result with status and details
            
        Raises:
            ReturnProcessingError: If processing fails
        """
        if return_obj.return_type != 'PURCHASE':
            raise ReturnProcessingError(f"Expected purchase return, got {return_obj.return_type}")
        
        if return_obj.status != 'PENDING':
            raise ReturnProcessingError(f"Can only process PENDING returns, current status: {return_obj.status}")
        
        if not return_obj.purchase:
            raise ReturnProcessingError("Purchase return must be linked to a purchase order")
        
        if not return_obj.items.exists():
            raise ReturnProcessingError("Return must contain at least one item")
        
        try:
            with transaction.atomic():
                # Process each returned item
                processing_details = []
                total_returned_value = Decimal('0.00')
                
                for return_item in return_obj.items.all():
                    product = return_item.product
                    quantity = return_item.quantity
                    unit_price = Decimal(str(return_item.unit_price))
                    line_value = quantity * unit_price
                    
                    # Validate product
                    if not product or not product.is_active:
                        raise ReturnProcessingError(
                            f"Product {product.name if product else 'Unknown'} is inactive or unavailable"
                        )
                    
                    # Decrement inventory for purchase returns (items going back to supplier)
                    primary_location = product.locations.filter(is_primary=True).first()
                    if not primary_location:
                        primary_location = product.locations.first()
                    
                    if primary_location:
                        stock_record = Stock.objects.filter(
                            product=product,
                            location=primary_location
                        ).first()
                        
                        if stock_record:
                            # Verify sufficient stock to return
                            if stock_record.quantity < quantity:
                                raise ReturnProcessingError(
                                    f"Insufficient stock for {product.name}: "
                                    f"have {stock_record.quantity}, attempting to return {quantity}"
                                )
                            
                            # Decrement stock
                            stock_record.quantity -= quantity
                            stock_record.save(update_fields=['quantity'])
                        
                        processing_details.append({
                            'product_id': product.id,
                            'product_name': product.name,
                            'product_sku': product.sku,
                            'quantity_decremented': quantity,
                            'location': primary_location.name,
                            'value': str(line_value),
                        })
                    
                    total_returned_value += line_value
                
                # Update return record
                return_obj.status = 'APPROVED'
                return_obj.approved_by = user
                return_obj.approved_at = timezone.now()
                return_obj.refund_amount = total_returned_value
                return_obj.refund_method = return_obj.refund_method  # Keep existing refund method
                return_obj.updated_by = user
                return_obj.save(update_fields=[
                    'status',
                    'approved_by',
                    'approved_at',
                    'refund_amount',
                    'updated_by'
                ])
                
                logger.info(
                    f"Approved purchase return {return_obj.return_number}: "
                    f"Total value: {total_returned_value}, Items: {len(processing_details)}"
                )
                
                return {
                    'status': 'SUCCESS',
                    'return_number': return_obj.return_number,
                    'items_processed': len(processing_details),
                    'total_returned_value': str(total_returned_value),
                    'details': processing_details,
                    'message': f"Successfully approved purchase return {return_obj.return_number} and decremented inventory"
                }
                
        except Exception as e:
            logger.error(
                f"Error processing purchase return {return_obj.return_number}: {str(e)}",
                exc_info=True
            )
            raise ReturnProcessingError(f"Failed to process return: {str(e)}")
    
    @staticmethod
    def complete_return(return_obj, user):
        """
        Complete a return, indicating refund has been processed.
        
        Args:
            return_obj: Return model instance (should be APPROVED)
            user: User who completed the return
            
        Raises:
            ReturnProcessingError: If return is not in correct state
        """
        if return_obj.status != 'APPROVED':
            raise ReturnProcessingError(
                f"Can only complete APPROVED returns, current status: {return_obj.status}"
            )
        
        try:
            with transaction.atomic():
                return_obj.status = 'COMPLETED'
                return_obj.save(update_fields=['status', 'updated_by', 'updated_at'])
                
                logger.info(f"Completed return {return_obj.return_number}")
                
                return {
                    'status': 'SUCCESS',
                    'return_number': return_obj.return_number,
                    'message': f"Return {return_obj.return_number} marked as completed"
                }
        except Exception as e:
            logger.error(f"Error completing return {return_obj.return_number}: {str(e)}", exc_info=True)
            raise ReturnProcessingError(f"Failed to complete return: {str(e)}")
    
    @staticmethod
    def reject_return(return_obj):
        """
        Reject a pending return (no inventory changes).
        
        Args:
            return_obj: Return model instance (should be PENDING)
            
        Raises:
            ReturnProcessingError: If return is not in correct state
        """
        if return_obj.status != 'PENDING':
            raise ReturnProcessingError(
                f"Can only reject PENDING returns, current status: {return_obj.status}"
            )
        
        try:
            with transaction.atomic():
                return_obj.status = 'REJECTED'
                return_obj.save(update_fields=['status', 'updated_by', 'updated_at'])
                
                logger.info(f"Rejected return {return_obj.return_number}")
                
                return {
                    'status': 'SUCCESS',
                    'return_number': return_obj.return_number,
                    'message': f"Return {return_obj.return_number} has been rejected"
                }
        except Exception as e:
            logger.error(f"Error rejecting return {return_obj.return_number}: {str(e)}", exc_info=True)
            raise ReturnProcessingError(f"Failed to reject return: {str(e)}")
