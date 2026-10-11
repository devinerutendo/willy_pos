"""
Utilities for stock transaction audit trail analysis.

Provides functions to:
- Query stock transactions by type, date range, or reference
- Generate audit reports for sales transactions
- Verify transaction consistency
- Analyze inventory movement patterns
"""

import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from django.db.models import Q, Sum, Count
from Inventory.models import StockTransaction, Stock, Product
from Sales.models import Sale, SaleItem

logger = logging.getLogger(__name__)


class AuditTrailAnalyzer:
    """
    Analyzer for stock transaction audit trails.
    Helps verify inventory reconciliation and transaction integrity.
    """
    
    @staticmethod
    def get_sale_transactions(sale_ref: str) -> List[Dict[str, Any]]:
        """
        Get all stock transactions related to a specific sale.
        
        Includes OUT transactions for the original sale and RETURN 
        transactions if the sale was cancelled.
        
        Args:
            sale_ref: Sale reference number (e.g., 'SALE-20260325120000-AB12')
            
        Returns:
            List of transaction dictionaries with complete details
        """
        logger.debug(f"Querying audit trail for sale {sale_ref}")
        
        try:
            # Query original sale transactions and any cancellations
            transactions = StockTransaction.objects.filter(
                Q(reference=sale_ref) | Q(reference=f"CANCELLED_{sale_ref}")
            ).select_related('product', 'location', 'created_by').order_by('created_at')
            
            if not transactions.exists():
                logger.warning(f"No transactions found for sale {sale_ref}")
                return []
            
            result = []
            for txn in transactions:
                result.append({
                    'id': txn.id,
                    'product': {
                        'id': txn.product.id,
                        'name': txn.product.name,
                        'sku': txn.product.sku
                    },
                    'location': {
                        'id': txn.location.id,
                        'name': txn.location.name
                    },
                    'type': txn.get_transaction_type_display(),
                    'type_code': txn.transaction_type,
                    'quantity': txn.quantity,
                    'reference': txn.reference,
                    'notes': txn.notes,
                    'created_at': txn.created_at,
                    'created_by': txn.created_by.full_name if txn.created_by else None
                })
            
            logger.info(
                f"Retrieved {len(result)} transactions for sale {sale_ref}",
                extra={"sale_ref": sale_ref, "transaction_count": len(result)}
            )
            return result
            
        except Exception as e:
            logger.exception(f"Error querying audit trail for sale {sale_ref}")
            return []
    
    @staticmethod
    def get_product_movement_history(
        product_id: int,
        days: int = 30,
        limit: int = 100
    ) -> Dict[str, Any]:
        """
        Get complete movement history for a product over specified timeframe.
        
        Args:
            product_id: Product ID to query
            days: Number of days to look back (default 30)
            limit: Maximum transactions to return (default 100)
            
        Returns:
            Dictionary with movement summary and transaction details
        """
        logger.debug(f"Querying movement history for product {product_id}")
        
        try:
            product = Product.objects.get(id=product_id)
            cutoff_date = datetime.now() - timedelta(days=days)
            
            transactions = StockTransaction.objects.filter(
                product=product,
                created_at__gte=cutoff_date
            ).select_related('location', 'created_by').order_by('-created_at')[:limit]
            
            # Calculate summary statistics
            summary = {
                'product_id': product.id,
                'product_name': product.name,
                'product_sku': product.sku,
                'period_days': days,
                'period_start': cutoff_date,
                'transaction_count': len(transactions),
                'by_type': {
                    'IN': 0,
                    'OUT': 0,
                    'RETURN': 0,
                    'ADJUSTMENT': 0
                },
                'quantity_in': 0,
                'quantity_out': 0,
                'quantity_returned': 0,
                'net_movement': 0,
                'transactions': []
            }
            
            for txn in transactions:
                # Update type counts
                summary['by_type'][txn.transaction_type] += 1
                
                # Calculate movements
                if txn.transaction_type == StockTransaction.IN:
                    summary['quantity_in'] += txn.quantity
                    summary['net_movement'] += txn.quantity
                elif txn.transaction_type == StockTransaction.OUT:
                    summary['quantity_out'] += abs(txn.quantity)
                    summary['net_movement'] += txn.quantity  # Already negative
                elif txn.transaction_type == StockTransaction.RETURN:
                    summary['quantity_returned'] += txn.quantity
                    summary['net_movement'] += txn.quantity
                
                summary['transactions'].append({
                    'id': txn.id,
                    'type': txn.get_transaction_type_display(),
                    'quantity': txn.quantity,
                    'reference': txn.reference,
                    'location': txn.location.name,
                    'created_at': txn.created_at,
                    'notes': txn.notes[:100] if txn.notes else None
                })
            
            logger.info(
                f"Retrieved movement history for product {product.name}: "
                f"{summary['transaction_count']} transactions over {days} days",
                extra={"product_id": product_id, "transaction_count": summary['transaction_count']}
            )
            return summary
            
        except Product.DoesNotExist:
            logger.error(f"Product {product_id} not found")
            return {}
        except Exception as e:
            logger.exception(f"Error querying movement history for product {product_id}")
            return {}
    
    @staticmethod
    def verify_sale_completeness(sale_ref: str) -> Dict[str, Any]:
        """
        Verify that all items in a sale have corresponding transactions.
        
        Checks that:
        1. Sale exists
        2. All sale items have OUT transactions
        3. Transaction quantities match sale quantities
        
        Args:
            sale_ref: Sale reference number
            
        Returns:
            Verification report with status and details
        """
        logger.debug(f"Verifying transaction completeness for sale {sale_ref}")
        
        try:
            sale = Sale.objects.get(reference=sale_ref)
            
            report = {
                'sale_id': sale.id,
                'sale_ref': sale.reference,
                'sale_status': sale.status,
                'is_complete': True,
                'missing_transactions': [],
                'mismatched_quantities': [],
                'total_items': sale.items.count(),
                'total_out_transactions': 0,
                'transaction_total_quantity': 0
            }
            
            sale_items = SaleItem.objects.filter(sale=sale)
            
            for sale_item in sale_items:
                # Look for OUT transaction for this product/sale
                out_txns = StockTransaction.objects.filter(
                    product=sale_item.product,
                    reference=sale_ref,
                    transaction_type=StockTransaction.OUT
                )
                
                if not out_txns.exists():
                    report['is_complete'] = False
                    report['missing_transactions'].append({
                        'product_id': sale_item.product.id,
                        'product_name': sale_item.product.name,
                        'expected_quantity': sale_item.quantity
                    })
                else:
                    report['total_out_transactions'] += 1
                    txn_quantity = abs(sum(t.quantity for t in out_txns))
                    report['transaction_total_quantity'] += txn_quantity
                    
                    if txn_quantity != sale_item.quantity:
                        report['is_complete'] = False
                        report['mismatched_quantities'].append({
                            'product_name': sale_item.product.name,
                            'expected': sale_item.quantity,
                            'found': txn_quantity
                        })
            
            if report['is_complete']:
                logger.info(
                    f"Sale {sale_ref} verification: COMPLETE - All {report['total_items']} "
                    f"items have matching transactions",
                    extra={"sale_ref": sale_ref, "status": "COMPLETE"}
                )
            else:
                logger.warning(
                    f"Sale {sale_ref} verification: INCOMPLETE - "
                    f"Missing: {len(report['missing_transactions'])}, "
                    f"Mismatched: {len(report['mismatched_quantities'])}",
                    extra={
                        "sale_ref": sale_ref,
                        "status": "INCOMPLETE",
                        "missing": len(report['missing_transactions']),
                        "mismatched": len(report['mismatched_quantities'])
                    }
                )
            
            return report
            
        except Sale.DoesNotExist:
            logger.error(f"Sale {sale_ref} not found")
            return {'error': f'Sale {sale_ref} not found', 'is_complete': False}
        except Exception as e:
            logger.exception(f"Error verifying sale {sale_ref}")
            return {'error': str(e), 'is_complete': False}
    
    @staticmethod
    def get_daily_summary(date: Optional[datetime] = None) -> Dict[str, Any]:
        """
        Get summary of all stock transactions for a specific date.
        
        Args:
            date: Date to summarize (default today)
            
        Returns:
            Summary with transaction counts and totals by type
        """
        if date is None:
            date = datetime.now()
        
        start_of_day = date.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = start_of_day + timedelta(days=1)
        
        transactions = StockTransaction.objects.filter(
            created_at__gte=start_of_day,
            created_at__lt=end_of_day
        )
        
        summary = {
            'date': date.date(),
            'total_transactions': transactions.count(),
            'by_type': {},
            'total_units_in': 0,
            'total_units_out': 0,
            'total_units_returned': 0,
            'unique_products': transactions.values('product').distinct().count(),
            'unique_locations': transactions.values('location').distinct().count()
        }
        
        for txn_type in [StockTransaction.IN, StockTransaction.OUT, StockTransaction.RETURN, StockTransaction.ADJUSTMENT]:
            type_txns = transactions.filter(transaction_type=txn_type)
            count = type_txns.count()
            total_qty = type_txns.aggregate(Sum('quantity'))['quantity__sum'] or 0
            
            summary['by_type'][txn_type] = {
                'display': dict(StockTransaction.TRANSACTION_TYPES).get(txn_type),
                'count': count,
                'total_quantity': total_qty
            }
            
            # Update movement totals
            if txn_type == StockTransaction.IN:
                summary['total_units_in'] = total_qty
            elif txn_type == StockTransaction.OUT:
                summary['total_units_out'] = abs(total_qty)
            elif txn_type == StockTransaction.RETURN:
                summary['total_units_returned'] = total_qty
        
        return summary
