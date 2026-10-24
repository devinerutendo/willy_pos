from rest_framework import viewsets, permissions, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from django_filters.rest_framework import DjangoFilterBackend
from django.db.models import F, Q, Sum, Prefetch
from django.utils import timezone
import logging
import uuid
from django.db import transaction
from Core.access import branch_scoped_queryset
from Core.permissions import ScopePermission
from .models import (
    Customer, Quotation, QuotationItem, Sale, SaleItem,
    Layby, LaybyItem, LaybyPayment, LaybyRefund, DailyReconciliation,
    Return, ReturnItem
)
from .serializers import (
    CustomerSerializer,
    QuotationSerializer, QuotationItemSerializer,
    SaleSerializer, SaleItemSerializer,
    InventoryVerificationSerializer,
    LaybySerializer, LaybyItemSerializer, LaybyPaymentSerializer, LaybyRefundSerializer,
    DailyReconciliationSerializer, ReturnSerializer, ReturnItemSerializer
)
from .return_processor import ReturnProcessor, ReturnProcessingError
from .reporting import build_sales_report
from .conversion import QuotationConversionError, convert_quotation_to_invoice
from .invoice_serializers import (
    InvoiceItemInputSerializer, InvoiceSerializer, InvoiceStatusHistorySerializer,
)
from . import invoicing
from .models import Invoice
from .tasks import complete_sale_inventory_task, _complete_sale_inventory


class BaseViewSet(viewsets.ModelViewSet):
    """Base viewset with common functionality for all sales views."""
    permission_classes = [permissions.IsAuthenticated, ScopePermission]
    permission_scope = 'sales'

    def _get_audit_save_kwargs(self, serializer, include_created=False):
        """Return only audit fields that actually exist on the serializer's model."""
        model = getattr(getattr(serializer, 'Meta', None), 'model', None)
        if not model:
            return {}

        field_names = {field.name for field in model._meta.get_fields()}
        save_kwargs = {}

        if include_created and 'created_by' in field_names:
            save_kwargs['created_by'] = self.request.user
        if 'updated_by' in field_names:
            save_kwargs['updated_by'] = self.request.user

        return save_kwargs
    
    def perform_create(self, serializer):
        """Auto-assign audit fields only for models that support them."""
        serializer.save(**self._get_audit_save_kwargs(serializer, include_created=True))
    
    def perform_update(self, serializer):
        """Auto-assign updated_by only where supported."""
        serializer.save(**self._get_audit_save_kwargs(serializer))


class CustomerViewSet(BaseViewSet):
    serializer_class = CustomerSerializer
    
    def get_queryset(self):
        """Optimized customer query with filtering."""
        queryset = Customer.objects.all().order_by('-created_at')
        
        # Search by name or email
        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search) | 
                Q(email__icontains=search) | 
                Q(phone__icontains=search)
            )
        
        # Filter by active status
        is_active = self.request.query_params.get('is_active')
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active.lower() == 'true')
        
        return queryset
    
    @action(detail=True, methods=['post'])
    def deactivate(self, request, pk=None):
        """Deactivate a customer."""
        customer = self.get_object()
        customer.is_active = False
        customer.save()
        return Response(
            {'detail': f'Customer {customer.name} deactivated successfully.'},
            status=status.HTTP_200_OK
        )
    
    @action(detail=True, methods=['post'])
    def activate(self, request, pk=None):
        """Activate a customer."""
        customer = self.get_object()
        customer.is_active = True
        customer.save()
        return Response(
            {'detail': f'Customer {customer.name} activated successfully.'},
            status=status.HTTP_200_OK
        )


class QuotationViewSet(BaseViewSet):
    serializer_class = QuotationSerializer
    
    def get_queryset(self):
        """Optimized quotation query with related items."""
        queryset = Quotation.objects.select_related(
            'customer',
            'branch',
            'pos_terminal',
            'created_by',
            'updated_by',
            'invoice',
            'converted_by',
        ).prefetch_related('items__product').order_by('-created_at')
        queryset = branch_scoped_queryset(queryset, self.request.user)
        
        # Filter by status
        status_filter = self.request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        
        # Filter by customer
        customer = self.request.query_params.get('customer')
        if customer:
            queryset = queryset.filter(customer_id=customer)

        branch = self.request.query_params.get('branch')
        if branch:
            queryset = queryset.filter(branch_id=branch)

        pos_terminal = self.request.query_params.get('pos_terminal')
        if pos_terminal:
            queryset = queryset.filter(pos_terminal_id=pos_terminal)
        
        # Filter by expiration status
        expiration = self.request.query_params.get('expiration')
        if expiration == 'expired':
            queryset = queryset.filter(valid_until__lt=timezone.now())
        elif expiration == 'active':
            queryset = queryset.filter(valid_until__gte=timezone.now())
        
        return queryset

    def perform_create(self, serializer):
        branch = serializer.validated_data.get('branch') or getattr(self.request.user, 'branch', None)
        pos_terminal = serializer.validated_data.get('pos_terminal')

        if pos_terminal and branch and pos_terminal.branch_id != branch.id:
            raise ValidationError({'pos_terminal': 'Selected POS terminal does not belong to the selected branch.'})
        if pos_terminal and branch is None:
            branch = pos_terminal.branch

        serializer.save(
            branch=branch,
            pos_terminal=pos_terminal,
            created_by=self.request.user,
            updated_by=self.request.user,
        )
    
    @staticmethod
    def _converted_conflict(quotation):
        return Response(
            {
                'code': 'already_converted',
                'detail': (
                    f'Quotation {quotation.reference} has been converted to invoice '
                    f'{quotation.invoice.reference} and can no longer be changed.'
                ),
            },
            status=status.HTTP_409_CONFLICT,
        )

    def update(self, request, *args, **kwargs):
        quotation = self.get_object()
        if quotation.is_converted:
            return self._converted_conflict(quotation)
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        quotation = self.get_object()
        if quotation.is_converted:
            return self._converted_conflict(quotation)
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=['post'])
    def convert(self, request, pk=None):
        """Convert an accepted quotation into a pending sales invoice.

        Requires the same ``sales`` write permission as creating a sale. Repeated
        requests return the existing invoice (HTTP 200, ``created: false``).
        """
        quotation = self.get_object()  # enforces branch scoping and permissions
        try:
            invoice, created = convert_quotation_to_invoice(quotation.pk, request.user)
        except QuotationConversionError as exc:
            return Response(exc.as_payload(), status=exc.status_code)

        invoice = invoice_queryset().get(pk=invoice.pk)
        quotation = self.get_queryset().get(pk=quotation.pk)
        message = (
            f'Quotation {quotation.reference} converted to invoice {invoice.reference}. '
            'It is pending approval; stock is deducted only when it is approved.'
            if created else
            f'Quotation {quotation.reference} was already converted to invoice {invoice.reference}.'
        )
        return Response(
            {
                'created': created,
                'detail': message,
                'invoice': InvoiceSerializer(invoice, context=self.get_serializer_context()).data,
                'quotation': self.get_serializer(quotation).data,
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @action(detail=True, methods=['post'])
    def accept(self, request, pk=None):
        """Accept a quotation."""
        quotation = self.get_object()
        if quotation.is_converted:
            raise ValidationError('This quotation has already been converted to an invoice.')
        if quotation.status == Quotation.ACCEPTED:
            raise ValidationError('This quotation has already been accepted.')
        if quotation.status == Quotation.REJECTED:
            raise ValidationError('Cannot accept a rejected quotation.')
        if quotation.is_expired:
            raise ValidationError('Cannot accept an expired quotation.')
        
        quotation.status = Quotation.ACCEPTED
        quotation.updated_by = request.user
        quotation.save()
        
        serializer = self.get_serializer(quotation)
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        """Reject a quotation."""
        quotation = self.get_object()
        if quotation.is_converted:
            raise ValidationError('This quotation has already been converted to an invoice.')
        if quotation.status == Quotation.REJECTED:
            raise ValidationError('This quotation is already rejected.')
        if quotation.status == Quotation.ACCEPTED:
            raise ValidationError('Cannot reject an accepted quotation.')
        
        quotation.status = Quotation.REJECTED
        quotation.updated_by = request.user
        quotation.save()
        
        serializer = self.get_serializer(quotation)
        return Response(serializer.data)


class QuotationItemViewSet(BaseViewSet):
    serializer_class = QuotationItemSerializer
    
    def get_queryset(self):
        """Optimized query with related quotations and products."""
        queryset = QuotationItem.objects.select_related(
            'quotation',
            'product'
        ).order_by('quotation__created_at')
        
        # Filter by quotation
        quotation = self.request.query_params.get('quotation')
        if quotation:
            queryset = queryset.filter(quotation_id=quotation)
        
        return queryset

    @staticmethod
    def _ensure_quotation_editable(quotation):
        if quotation.is_converted:
            raise ValidationError(
                f'Quotation {quotation.reference} has been converted to an invoice and its items can no longer be changed.'
            )

    def perform_create(self, serializer):
        self._ensure_quotation_editable(serializer.validated_data['quotation'])
        super().perform_create(serializer)

    def perform_update(self, serializer):
        self._ensure_quotation_editable(serializer.instance.quotation)
        self._ensure_quotation_editable(serializer.validated_data.get('quotation', serializer.instance.quotation))
        super().perform_update(serializer)

    def perform_destroy(self, instance):
        self._ensure_quotation_editable(instance.quotation)
        super().perform_destroy(instance)


class SaleViewSet(BaseViewSet):
    serializer_class = SaleSerializer
    
    def get_queryset(self):
        """Optimized sale query with related data."""
        queryset = Sale.objects.select_related(
            'customer',
            'sold_by',
            'branch',
            'pos_terminal',
            'created_by',
            'updated_by',
            'source_invoice',
            'legacy_source_quotation',
        ).prefetch_related('items__product').order_by('-created_at')
        queryset = branch_scoped_queryset(queryset, self.request.user)
        
        # Filter by status
        status_filter = self.request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        
        # Filter by customer
        customer = self.request.query_params.get('customer')
        if customer:
            queryset = queryset.filter(customer_id=customer)
        
        # Filter by sales person (sold_by)
        sold_by = self.request.query_params.get('sold_by')
        if sold_by:
            queryset = queryset.filter(sold_by_id=sold_by)

        # Filter by branch
        branch = self.request.query_params.get('branch')
        if branch:
            queryset = queryset.filter(branch_id=branch)

        # Filter by POS terminal
        pos_terminal = self.request.query_params.get('pos_terminal')
        if pos_terminal:
            queryset = queryset.filter(pos_terminal_id=pos_terminal)
        
        # Filter by date range
        start_date = self.request.query_params.get('start_date')
        end_date = self.request.query_params.get('end_date')
        if start_date:
            queryset = queryset.filter(created_at__date__gte=start_date)
        if end_date:
            queryset = queryset.filter(created_at__date__lte=end_date)
        
        return queryset
    
    def perform_create(self, serializer):
        """
        Auto-assign sold_by and audit fields.
        Auto-generate reference and create sales as PENDING for confirmation.
        """
        # Generate reference if not provided
        reference = serializer.validated_data.get('reference')
        if not reference:
            reference = Sale.generate_reference()
        
        # Save sale with auto-assigned fields as PENDING status
        branch = serializer.validated_data.get('branch') or getattr(self.request.user, 'branch', None)
        pos_terminal = serializer.validated_data.get('pos_terminal')

        if pos_terminal and branch and pos_terminal.branch_id != branch.id:
            raise ValidationError({'pos_terminal': 'Selected POS terminal does not belong to the selected branch.'})
        if pos_terminal and branch is None:
            branch = pos_terminal.branch

        serializer.save(
            reference=reference,
            sold_by=self.request.user,
            branch=branch,
            pos_terminal=pos_terminal,
            created_by=self.request.user,
            updated_by=self.request.user,
            status=Sale.PENDING  # Create as PENDING, require explicit confirmation
        )

    def destroy(self, request, *args, **kwargs):
        sale = self.get_object()
        invoice = Invoice.objects.filter(sale=sale).first()
        if invoice:
            return Response(
                {
                    'code': 'sale_linked_to_invoice',
                    'detail': (
                        f'Sale {sale.reference} was created by approving invoice {invoice.reference} '
                        'and cannot be deleted.'
                    ),
                },
                status=status.HTTP_409_CONFLICT,
            )
        quotation = Quotation.objects.filter(legacy_sale=sale).first()
        if quotation:
            return Response(
                {
                    'code': 'invoice_linked_to_quotation',
                    'detail': (
                        f'Invoice {sale.reference} was created from quotation {quotation.reference} '
                        'and cannot be deleted. Cancel it instead.'
                    ),
                },
                status=status.HTTP_409_CONFLICT,
            )
        return super().destroy(request, *args, **kwargs)

    @action(detail=False, methods=['get'])
    def report(self, request):
        """Aggregated sales report: summary, trend, previous period and transaction rows.

        Query params: start_date, end_date (YYYY-MM-DD), status, payment_method,
        sold_by, branch, search.
        """
        return Response(build_sales_report(request.user, request.query_params))
    
    @action(detail=True, methods=['post'])
    def confirm(self, request, pk=None):
        """
        Confirm and complete a pending sale with async inventory deduction.
        
        This endpoint triggers a Celery async task to deduct inventory and returns
        immediately with a task ID, enabling efficient POS transactions.
        
        Request body (optional):
            {
                "payment_method": "CASH|CARD|etc",
                "amount_received": decimal (required for CASH),
                "change_amount": decimal (calculated if not provided)
            }
        
        Response includes:
            - Sale data with COMPLETED status
            - Celery task ID for tracking
            - Frontend should poll task status to verify inventory completion
        
        Returns:
            - Sale response with task tracking info
        """
        from .tasks import complete_sale_inventory_task
        
        logger = logging.getLogger(__name__)
        sale = self.get_object()
        
        logger.info(
            f"Confirm request for sale {sale.reference} (ID: {sale.id})",
            extra={"sale_id": sale.id, "sale_ref": sale.reference}
        )
        
        # Update payment information if provided
        payment_method = request.data.get('payment_method')
        amount_received = request.data.get('amount_received')
        change_amount = request.data.get('change_amount')
        
        if payment_method:
            sale.payment_method = payment_method
            logger.debug(f"Payment method set to: {payment_method}", extra={"sale_id": sale.id})
        
        if payment_method == 'CASH' and amount_received is not None:
            sale.amount_received = amount_received
            logger.debug(f"Amount received: {amount_received}", extra={"sale_id": sale.id})
        
        if change_amount is not None:
            sale.change_amount = change_amount
        else:
            # Calculate change if not provided
            if payment_method == 'CASH' and amount_received is not None:
                total = sale.total_amount
                sale.change_amount = amount_received - total
        
        # Validate current status
        if sale.status == Sale.COMPLETED:
            return Response(
                {'detail': 'Sale is already confirmed and completed.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if sale.status == Sale.CANCELLED:
            return Response(
                {'detail': 'Cannot confirm a cancelled sale.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Validate sale has items
        if not sale.items.exists():
            return Response(
                {'detail': 'Cannot confirm sale without items.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            with transaction.atomic():
                logger.debug(
                    f"Marking sale {sale.reference} as COMPLETED",
                    extra={"sale_id": sale.id, "items_count": sale.items.count()}
                )
                
                # Mark sale as COMPLETED and save payment info
                sale.status = Sale.COMPLETED
                sale.payment_status = Sale.PAID
                sale.inventory_sync_status = 'PENDING'
                sale.inventory_completed = False
                sale.updated_by = request.user
                sale.save()
                
                logger.info(
                    f"Sale {sale.reference} marked COMPLETED. Queuing async inventory task.",
                    extra={"sale_id": sale.id}
                )
                
                # Queue async inventory deduction task
                try:
                    task = complete_sale_inventory_task.delay(sale.id)
                    sale.celery_task_id = task.id
                    sale.save(update_fields=['celery_task_id'])

                    logger.info(
                        f"Inventory task queued for sale {sale.reference}. Task ID: {task.id}",
                        extra={"sale_id": sale.id, "task_id": task.id}
                    )
                except Exception as task_error:
                    logger.error(
                        f"Failed to queue inventory task for sale {sale.reference}: {str(task_error)}",
                        extra={"sale_id": sale.id},
                        exc_info=True
                    )
                    sale.inventory_sync_status = 'FAILED'
                    sale.save(update_fields=['inventory_sync_status'])

                    try:
                        logger.warning(
                            f"Falling back to synchronous inventory completion for sale {sale.reference}.",
                            extra={"sale_id": sale.id}
                        )
                        fallback_result = _complete_sale_inventory(sale.id)
                        if fallback_result.get('success'):
                            sale.inventory_completed = True
                            sale.inventory_sync_status = 'SUCCESS'
                            sale.save(update_fields=['inventory_completed', 'inventory_sync_status'])
                            logger.info(
                                f"Synchronous inventory completion succeeded for sale {sale.reference}.",
                                extra={"sale_id": sale.id}
                            )
                        else:
                            logger.error(
                                f"Synchronous inventory completion failed for sale {sale.reference}: {fallback_result.get('error')}",
                                extra={"sale_id": sale.id}
                            )
                    except Exception as fallback_error:
                        logger.exception(
                            f"Synchronous inventory completion fallback failed for sale {sale.reference}: {str(fallback_error)}",
                            extra={"sale_id": sale.id}
                        )

                # Return response with task tracking info
                serializer = self.get_serializer(sale)
                if not sale.celery_task_id and sale.inventory_completed:
                    message = 'Inventory deduction completed successfully.'
                elif not sale.celery_task_id and sale.inventory_sync_status == 'FAILED':
                    message = 'Inventory deduction failed. Please review inventory and retry.'
                else:
                    message = 'Inventory deduction in progress. Check task status for completion.'

                response_data = {
                    'detail': f'Sale {sale.reference} completed successfully.',
                    'sale': serializer.data,
                    'celery_task_id': sale.celery_task_id,
                    'inventory_sync_status': sale.inventory_sync_status,
                    'inventory_completed': sale.inventory_completed,
                    'message': message,
                }
                
                return Response(response_data, status=status.HTTP_200_OK)
                
        except Exception as e:
            logger.exception(
                f"Error confirming sale {sale.reference} (ID: {sale.id}): {str(e)}",
                extra={"sale_id": sale.id, "sale_ref": sale.reference}
            )
            return Response(
                {
                    'detail': f'Error confirming sale: {str(e)}',
                    'error_type': type(e).__name__
                },
                status=status.HTTP_400_BAD_REQUEST
            )


    
    @action(detail=True, methods=['post'])
    def complete(self, request, pk=None):
        """Mark a sale as completed (alias for confirm)."""
        return self.confirm(request, pk)
    
    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Cancel a sale."""
        sale = self.get_object()
        if sale.status == Sale.CANCELLED:
            raise ValidationError('This sale is already cancelled.')
        if sale.status == Sale.COMPLETED:
            raise ValidationError('Cannot cancel a completed sale.')
        
        sale.status = Sale.CANCELLED
        sale.updated_by = request.user
        sale.save()
        
        serializer = self.get_serializer(sale)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def today(self, request):
        """Get sales from today."""
        today = timezone.now().date()
        queryset = self.get_queryset().filter(created_at__date=today)
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def summary(self, request):
        """Get sales summary statistics."""
        queryset = self.get_queryset()
        
        today = timezone.now().date()
        today_sales = queryset.filter(created_at__date=today)
        
        summary = {
            'total_today': {
                'count': today_sales.filter(status=Sale.COMPLETED).count(),
                'amount': 0
            },
            'pending': {
                'count': queryset.filter(status=Sale.PENDING).count(),
                'amount': 0
            },
            'completed': {
                'count': queryset.filter(status=Sale.COMPLETED).count(),
                'amount': 0
            },
            'cancelled': {
                'count': queryset.filter(status=Sale.CANCELLED).count(),
            }
        }
        
        return Response(summary)
    
    @action(detail=True, methods=['get'])
    def check_inventory_task(self, request, pk=None):
        """
        Check status of async inventory completion task.
        
        This endpoint polls the Celery task to verify when inventory
        deduction has completed after a sale is confirmed.
        
        Returns:
            Task status: PENDING, SUCCESS, FAILURE
            Task result with item details if completed
            Inventory verification status
        """
        from .tasks import complete_sale_inventory_task
        from celery.result import AsyncResult
        
        logger = logging.getLogger(__name__)
        sale = self.get_object()
        
        logger.debug(
            f"Checking inventory task for sale {sale.reference} (ID: {sale.id})",
            extra={"sale_id": sale.id, "task_id": sale.celery_task_id}
        )
        
        if not sale.celery_task_id:
            if sale.inventory_completed:
                return Response(
                    {
                        'task_id': None,
                        'status': 'SUCCESS',
                        'message': 'Inventory completed synchronously.',
                        'inventory_completed': sale.inventory_completed,
                        'inventory_sync_status': sale.inventory_sync_status,
                    },
                    status=status.HTTP_200_OK
                )
            return Response(
                {
                    'task_id': None,
                    'status': 'NO_TASK',
                    'message': 'No async inventory task associated with this sale',
                    'inventory_completed': sale.inventory_completed,
                    'inventory_sync_status': sale.inventory_sync_status,
                },
                status=status.HTTP_404_NOT_FOUND
            )
        
        try:
            # Get task result
            task_result = AsyncResult(sale.celery_task_id)
            task_status = task_result.status
            task_result_data = task_result.result if hasattr(task_result, 'result') else None
            
            logger.debug(
                f"Task {sale.celery_task_id} status: {task_status}",
                extra={"sale_id": sale.id, "task_id": sale.celery_task_id}
            )
            
            response_data = {
                'task_id': sale.celery_task_id,
                'status': task_status,
                'inventory_completed': sale.inventory_completed,
                'inventory_sync_status': sale.inventory_sync_status,
            }
            
            # If task is done, include result and verification
            if task_status in ['SUCCESS', 'FAILURE']:
                response_data['task_result'] = task_result_data
                
                if sale.inventory_completed:
                    # Perform verification now that task is done
                    verification_data = self._build_inventory_verification(sale)
                    response_data['verification'] = InventoryVerificationSerializer(verification_data).data
                    
                    # Update sync status based on verification
                    if verification_data['overall_status'] == 'VERIFIED':
                        if sale.inventory_sync_status != 'SUCCESS':
                            sale.inventory_sync_status = 'SUCCESS'
                            sale.save(update_fields=['inventory_sync_status'])
                            logger.info(
                                f"Inventory verification SUCCESS for sale {sale.reference}",
                                extra={"sale_id": sale.id}
                            )
                    else:
                        if sale.inventory_sync_status != 'FAILED':
                            sale.inventory_sync_status = 'FAILED'
                            sale.save(update_fields=['inventory_sync_status'])
                            logger.warning(
                                f"Inventory verification FAILED for sale {sale.reference}: "
                                f"{verification_data['overall_status']}",
                                extra={"sale_id": sale.id}
                            )
            
            return Response(response_data, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.exception(
                f"Error checking task status for sale {sale.reference} (ID: {sale.id})",
                extra={"sale_id": sale.id},
                exc_info=True
            )
            return Response(
                {
                    'task_id': sale.celery_task_id,
                    'error': f'Error checking task status: {str(e)}'
                },
                status=status.HTTP_400_BAD_REQUEST
            )
    
    @action(detail=True, methods=['get'])
    def verify_inventory(self, request, pk=None):
        """
        Verify that inventory was properly deducted for a completed sale.
        
        This endpoint provides a detailed audit of inventory state, comparing:
        - What should have been deducted (sale items)
        - What actually exists in stock (current quantities)
        
        Response includes item-by-item breakdown and overall verification status.
        
        QueryParams:
            - None
            
        Returns:
            Comprehensive inventory verification report with:
            - Overall status (VERIFIED, PARTIAL, FAILED)
            - Item-by-item verification
            - Discrepancies if any
            - Recommendations if issues found
        """
        logger = logging.getLogger(__name__)
        sale = self.get_object()
        
        logger.info(
            f"Inventory verification requested for sale {sale.reference} (ID: {sale.id})",
            extra={"sale_id": sale.id, "sale_ref": sale.reference}
        )
        
        try:
            verification_data = self._build_inventory_verification(sale)
            logger.info(
                f"Inventory verification completed for sale {sale.reference}: "
                f"Status={verification_data['overall_status']}, "
                f"Verified={verification_data['verified_items']}, "
                f"Mismatches={verification_data['mismatch_items']}",
                extra={"sale_id": sale.id, "sale_ref": sale.reference}
            )
            
            serializer = InventoryVerificationSerializer(verification_data)
            return Response(serializer.data, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.exception(
                f"Error during inventory verification for sale {sale.reference} (ID: {sale.id})",
                extra={"sale_id": sale.id, "sale_ref": sale.reference}
            )
            return Response(
                {'detail': f'Error verifying inventory: {str(e)}'},
                status=status.HTTP_400_BAD_REQUEST
            )
    
    def _build_inventory_verification(self, sale):
        """
        Build comprehensive inventory verification report for a sale.
        
        Args:
            sale: Sale instance to verify
            
        Returns:
            Dictionary with verification details
        """
        from Inventory.models import Stock
        
        verification_items = []
        verified_count = 0
        mismatch_count = 0
        missing_count = 0
        recommendations = []
        
        # Process each item in the sale
        for sale_item in sale.items.all():
            product = sale_item.product
            sale_qty = sale_item.quantity
            sale_price = sale_item.unit_price
            
            # Get all stock records for this product across all locations
            stock_records = Stock.objects.filter(product=product)
            
            # Build location breakdown
            stock_by_location = []
            total_current_quantity = 0
            total_reserved = 0
            
            for stock in stock_records:
                location_data = {
                    'location': stock.location.name,
                    'location_id': stock.location.id,
                    'quantity': stock.quantity,
                    'reserved': stock.reserved_quantity,
                    'available': stock.available_stock
                }
                stock_by_location.append(location_data)
                total_current_quantity += stock.quantity
                total_reserved += stock.reserved_quantity
            
            # Determine verification status
            if not stock_records.exists():
                status_val = 'MISSING'
                missing_count += 1
                discrepancy = sale_qty  # Expected to be gone entirely
                note = "No stock records found for this product"
                recommendations.append(
                    f"Product '{product.name}' has no stock tracking. "
                    f"Verify {sale_qty} units were properly accounted for."
                )
            else:
                # For COMPLETED sales, stock should be reduced by sale_qty
                # For PENDING sales, stock should be reserved
                if sale.status == Sale.COMPLETED:
                    # Stock should have been deducted during completion
                    # We can't definitively say what was deducted since we don't track
                    # the before/after, but we can see current state
                    status_val = 'VERIFIED'  # Assume correct unless reserved is wrong
                    discrepancy = 0
                    note = "Stock appears consistent with completed sale"
                    
                    # Check if reserved is unexpectedly high (suggests incomplete deduction)
                    if total_reserved > 0:
                        status_val = 'MISMATCH'
                        discrepancy = total_reserved
                        note = f"Warning: {total_reserved} units still reserved after completion"
                        mismatch_count += 1
                        recommendations.append(
                            f"Product '{product.name}' has {total_reserved} reserved units. "
                            f"Verify completion signal was processed correctly."
                        )
                    else:
                        verified_count += 1
                else:
                    # For PENDING sales, stock should be reserved
                    if total_reserved >= sale_qty:
                        status_val = 'VERIFIED'
                        discrepancy = 0
                        note = "Stock properly reserved for pending sale"
                        verified_count += 1
                    else:
                        status_val = 'MISMATCH'
                        discrepancy = sale_qty - total_reserved
                        note = f"Only {total_reserved} of {sale_qty} units reserved"
                        mismatch_count += 1
                        recommendations.append(
                            f"Product '{product.name}' reservation incomplete. "
                            f"Expected {sale_qty} reserved, found {total_reserved}."
                        )
            
            # Build item verification data
            item_verification = {
                'product_id': product.id,
                'product_name': product.name,
                'product_sku': product.sku,
                'expected_quantity_sold': sale_qty,
                'expected_unit_price': sale_price,
                'expected_total': sale_qty * sale_price,
                'current_stock_quantity': total_current_quantity,
                'current_reserved_quantity': total_reserved,
                'current_available_quantity': total_current_quantity - total_reserved,
                'stock_by_location': stock_by_location,
                'verification_status': status_val,
                'discrepancy': discrepancy,
                'notes': note
            }
            
            verification_items.append(item_verification)
        
        # Determine overall status
        if missing_count > 0:
            overall_status = 'FAILED'
        elif mismatch_count > 0:
            overall_status = 'PARTIAL'
        else:
            overall_status = 'VERIFIED'
        
        # Add recommendations based on overall status
        if overall_status != 'VERIFIED':
            if sale.status == Sale.COMPLETED:
                recommendations.append(
                    "Run the 'check_sale_sync' management command for detailed diagnostics"
                )
                recommendations.append(
                    "Review logs/signals.log for signal handler errors during completion"
                )
            
            if overall_status == 'FAILED':
                recommendations.append(
                    "Manually adjust inventory to match sale records"
                )
        
        return {
            'sale_id': sale.id,
            'sale_reference': sale.reference,
            'sale_status': sale.status,
            'sale_created_at': sale.created_at,
            'verification_timestamp': timezone.now(),
            'overall_status': overall_status,
            'verified_items': verified_count,
            'mismatch_items': mismatch_count,
            'missing_items': missing_count,
            'total_sale_amount': sale.total_amount,
            'items': verification_items,
            'all_inventory_deducted': overall_status == 'VERIFIED' and sale.status == Sale.COMPLETED,
            'requires_manual_review': overall_status != 'VERIFIED',
            'recommendations': recommendations
        }


class SaleItemViewSet(BaseViewSet):
    serializer_class = SaleItemSerializer
    
    def get_queryset(self):
        """Optimized query with related sales and products."""
        queryset = SaleItem.objects.select_related(
            'sale',
            'product'
        ).order_by('sale__created_at')
        
        # Filter by sale
        sale = self.request.query_params.get('sale')
        if sale:
            queryset = queryset.filter(sale_id=sale)
        
        return queryset


class LaybyViewSet(BaseViewSet):
    serializer_class = LaybySerializer
    
    def get_queryset(self):
        """Optimized layby query with related data."""
        queryset = Layby.objects.select_related(
            'customer',
            'branch',
            'pos_terminal',
            'created_by',
            'updated_by'
        ).prefetch_related(
            'items__product',
            'payments'
        ).order_by('-created_at')
        queryset = branch_scoped_queryset(queryset, self.request.user)
        
        # Filter by status
        status_filter = self.request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        
        # Filter by customer
        customer = self.request.query_params.get('customer')
        if customer:
            queryset = queryset.filter(customer_id=customer)

        branch = self.request.query_params.get('branch')
        if branch:
            queryset = queryset.filter(branch_id=branch)

        pos_terminal = self.request.query_params.get('pos_terminal')
        if pos_terminal:
            queryset = queryset.filter(pos_terminal_id=pos_terminal)
        
        # Filter by overdue status
        overdue = self.request.query_params.get('overdue')
        if overdue and overdue.lower() == 'true':
            queryset = queryset.filter(
                status=Layby.ACTIVE,
                due_date__lt=timezone.now()
            )
        
        return queryset
    
    def perform_create(self, serializer):
        """Set created_by and updated_by on layby creation."""
        branch = serializer.validated_data.get('branch') or getattr(self.request.user, 'branch', None)
        pos_terminal = serializer.validated_data.get('pos_terminal')

        if pos_terminal and branch and pos_terminal.branch_id != branch.id:
            raise ValidationError({'pos_terminal': 'Selected POS terminal does not belong to the selected branch.'})
        if pos_terminal and branch is None:
            branch = pos_terminal.branch

        serializer.save(
            branch=branch,
            pos_terminal=pos_terminal,
            created_by=self.request.user,
            updated_by=self.request.user,
        )
    
    @action(detail=True, methods=['post'])
    def complete(self, request, pk=None):
        """Mark a layby as completed."""
        layby = self.get_object()
        if layby.status == Layby.COMPLETED:
            raise ValidationError('This layby is already completed.')
        
        # Check if fully paid
        total_paid = sum(p.amount for p in layby.payments.all())
        total_amount = sum(item.total_price for item in layby.items.all())
        
        if total_paid < total_amount:
            raise ValidationError(
                f'Layby is not fully paid. Balance: {total_amount - total_paid}'
            )
        
        layby.status = Layby.COMPLETED
        layby.updated_by = request.user
        layby.save()
        
        serializer = self.get_serializer(layby)
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Cancel a layby."""
        layby = self.get_object()
        if layby.status == Layby.CANCELLED:
            raise ValidationError('This layby is already cancelled.')
        if layby.status == Layby.REFUNDED:
            raise ValidationError('Cannot cancel a refunded layby.')
        
        layby.status = Layby.CANCELLED
        layby.updated_by = request.user
        layby.save()
        
        serializer = self.get_serializer(layby)
        return Response(serializer.data)


class LaybyItemViewSet(BaseViewSet):
    serializer_class = LaybyItemSerializer
    
    def get_queryset(self):
        """Optimized query with related layby and product."""
        queryset = LaybyItem.objects.select_related(
            'layby',
            'product'
        ).order_by('layby__created_at')
        
        # Filter by layby
        layby = self.request.query_params.get('layby')
        if layby:
            queryset = queryset.filter(layby_id=layby)
        
        return queryset


class LaybyPaymentViewSet(BaseViewSet):
    serializer_class = LaybyPaymentSerializer
    
    def get_queryset(self):
        """Optimized query with related layby."""
        queryset = LaybyPayment.objects.select_related(
            'layby',
            'branch',
            'pos_terminal',
            'created_by'
        ).order_by('-created_at')
        queryset = branch_scoped_queryset(queryset, self.request.user)
        
        # Filter by layby
        layby = self.request.query_params.get('layby')
        if layby:
            queryset = queryset.filter(layby_id=layby)

        branch = self.request.query_params.get('branch')
        if branch:
            queryset = queryset.filter(branch_id=branch)

        pos_terminal = self.request.query_params.get('pos_terminal')
        if pos_terminal:
            queryset = queryset.filter(pos_terminal_id=pos_terminal)
        
        return queryset
    
    def perform_create(self, serializer):
        """Auto-assign created_by and updated_by fields for payment."""
        layby = serializer.validated_data['layby']
        branch = serializer.validated_data.get('branch') or getattr(self.request.user, 'branch', None) or layby.branch
        pos_terminal = serializer.validated_data.get('pos_terminal')

        if pos_terminal and branch and pos_terminal.branch_id != branch.id:
            raise ValidationError({'pos_terminal': 'Selected POS terminal does not belong to the selected branch.'})
        if pos_terminal and branch is None:
            branch = pos_terminal.branch

        serializer.save(
            branch=branch,
            pos_terminal=pos_terminal,
            created_by=self.request.user,
            updated_by=self.request.user,
        )


class LaybyRefundViewSet(BaseViewSet):
    serializer_class = LaybyRefundSerializer
    
    def get_queryset(self):
        """Optimized query with related layby."""
        queryset = LaybyRefund.objects.select_related(
            'layby',
            'created_by'
        ).order_by('-created_at')
        
        # Filter by layby
        layby = self.request.query_params.get('layby')
        if layby:
            queryset = queryset.filter(layby_id=layby)
        
        return queryset
    
    def perform_create(self, serializer):
        """Auto-assign created_by and updated_by fields and update layby status."""
        layby = serializer.validated_data['layby']
        refund = serializer.save(created_by=self.request.user, updated_by=self.request.user)
        
        # Update layby status to REFUNDED
        layby.status = Layby.REFUNDED
        layby.updated_by = self.request.user
        layby.save()


class DailyReconciliationViewSet(BaseViewSet):
    """ViewSet for managing daily reconciliation reports."""
    serializer_class = DailyReconciliationSerializer
    
    def get_queryset(self):
        """Optimized query with related users."""
        queryset = DailyReconciliation.objects.select_related(
            'prepared_by',
            'verified_by',
            'created_by',
            'updated_by'
        ).order_by('-reconciliation_date')
        
        # Filter by date range
        date_from = self.request.query_params.get('date_from')
        date_to = self.request.query_params.get('date_to')
        if date_from:
            queryset = queryset.filter(reconciliation_date__gte=date_from)
        if date_to:
            queryset = queryset.filter(reconciliation_date__lte=date_to)
        
        # Filter by locked status
        is_locked = self.request.query_params.get('is_locked')
        if is_locked is not None:
            queryset = queryset.filter(is_locked=is_locked.lower() == 'true')
        
        return queryset
    
    @action(detail=True, methods=['post'])
    def lock_reconciliation(self, request, pk=None):
        """Lock a reconciliation for audit trail."""
        reconciliation = self.get_object()
        
        if reconciliation.is_locked:
            return Response(
                {'detail': 'This reconciliation is already locked.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Validate that counted_cash is provided
        if reconciliation.counted_cash is None:
            return Response(
                {'detail': 'Counted cash must be entered before locking.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            reconciliation.lock_reconciliation(user=request.user)
            serializer = self.get_serializer(reconciliation)
            return Response(
                {'detail': 'Reconciliation locked successfully.', 'data': serializer.data},
                status=status.HTTP_200_OK
            )
        except Exception as e:
            logger = logging.getLogger(__name__)
            logger.exception(f"Error locking reconciliation {pk}: {str(e)}")
            return Response(
                {'detail': f'Error locking reconciliation: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=False, methods=['post'])
    def generate_today(self, request):
        """Generate or retrieve today's reconciliation."""
        from .tasks import generate_daily_reconciliation_task
        from datetime import datetime
        
        date_str = request.data.get('date', None)
        
        try:
            task_result = generate_daily_reconciliation_task.delay(
                user_id=request.user.id,
                reconciliation_date=date_str
            )
            
            return Response(
                {
                    'detail': 'Reconciliation generation started',
                    'task_id': task_result.id,
                    'status': 'PENDING'
                },
                status=status.HTTP_202_ACCEPTED
            )
        except Exception as e:
            logger = logging.getLogger(__name__)
            logger.exception(f"Error generating reconciliation: {str(e)}")
            return Response(
                {'detail': f'Error generating reconciliation: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class ReturnViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing product returns (sales and purchases).
    
    Provides endpoints for:
    - List all returns with filtering
    - Create new returns
    - Retrieve individual return details
    - Update return information
    - Approve returns
    - Reject returns
    - Complete returns
    """
    serializer_class = ReturnSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['return_type', 'status', 'customer', 'created_at']
    search_fields = ['return_number', 'customer__name', 'supplier__name', 'reason']
    ordering_fields = ['created_at', 'return_number', 'total_amount']
    ordering = ['-created_at']

    def get_queryset(self):
        queryset = Return.objects.select_related(
            'sale',
            'purchase',
            'customer',
            'supplier',
            'branch',
            'pos_terminal',
            'approved_by',
            'created_by',
            'updated_by',
        ).prefetch_related('items').order_by('-created_at')
        queryset = branch_scoped_queryset(queryset, self.request.user)

        branch = self.request.query_params.get('branch')
        if branch:
            queryset = queryset.filter(branch_id=branch)

        pos_terminal = self.request.query_params.get('pos_terminal')
        if pos_terminal:
            queryset = queryset.filter(pos_terminal_id=pos_terminal)

        return queryset
    
    def perform_create(self, serializer):
        """Set created_by and updated_by to current user."""
        sale = serializer.validated_data.get('sale')
        purchase = serializer.validated_data.get('purchase')
        branch = serializer.validated_data.get('branch') or getattr(self.request.user, 'branch', None)
        pos_terminal = serializer.validated_data.get('pos_terminal')

        if sale:
            branch = sale.branch or branch
            pos_terminal = sale.pos_terminal or pos_terminal
        elif purchase and getattr(purchase, 'branch_id', None):
            branch = purchase.branch or branch

        if pos_terminal and branch and pos_terminal.branch_id != branch.id:
            raise ValidationError({'pos_terminal': 'Selected POS terminal does not belong to the selected branch.'})
        if pos_terminal and branch is None:
            branch = pos_terminal.branch

        serializer.save(
            branch=branch,
            pos_terminal=pos_terminal,
            created_by=self.request.user,
            updated_by=self.request.user
        )
    
    def perform_update(self, serializer):
        """Set updated_by to current user."""
        serializer.save(updated_by=self.request.user)
    
    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        """
        Approve a return and process inventory/sales synchronization.
        
        For SALES returns:
        - Increments inventory for returned items
        - Records refund amount
        - Updates sale return status
        
        For PURCHASE returns:
        - Decrements inventory for returned items
        - Records refund amount  
        - Updates purchase return status
        
        Body: {}
        Returns: Processing result with inventory updates
        """
        return_obj = self.get_object()
        
        if return_obj.status != 'PENDING':
            return Response(
                {'detail': f'Cannot approve return with status {return_obj.status}'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            # Route to appropriate processor based on return type
            if return_obj.return_type == 'SALES':
                result = ReturnProcessor.process_sales_return(return_obj, request.user)
            elif return_obj.return_type == 'PURCHASE':
                result = ReturnProcessor.process_purchase_return(return_obj, request.user)
            else:
                return Response(
                    {'detail': f'Unknown return type: {return_obj.return_type}'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Refresh from DB after processing
            return_obj.refresh_from_db()
            serializer = self.get_serializer(return_obj)
            
            return Response(
                {
                    'detail': 'Return approved successfully',
                    'processing': result,
                    'data': serializer.data
                },
                status=status.HTTP_200_OK
            )
        except ReturnProcessingError as e:
            logger = logging.getLogger(__name__)
            logger.exception(f"Return processing error for {pk}: {str(e)}")
            return Response(
                {'detail': f'Processing error: {str(e)}'},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            logger = logging.getLogger(__name__)
            logger.exception(f"Error approving return {pk}: {str(e)}")
            return Response(
                {'detail': f'Error approving return: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        """
        Reject a return (no inventory changes).
        
        Body: {}
        Returns: Updated return with status=REJECTED
        """
        return_obj = self.get_object()
        
        if return_obj.status != 'PENDING':
            return Response(
                {'detail': f'Cannot reject return with status {return_obj.status}'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            result = ReturnProcessor.reject_return(return_obj)
            return_obj.refresh_from_db()
            serializer = self.get_serializer(return_obj)
            return Response(
                {
                    'detail': 'Return rejected successfully',
                    'processing': result,
                    'data': serializer.data
                },
                status=status.HTTP_200_OK
            )
        except ReturnProcessingError as e:
            logger = logging.getLogger(__name__)
            logger.exception(f"Return rejection error for {pk}: {str(e)}")
            return Response(
                {'detail': f'Error: {str(e)}'},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            logger = logging.getLogger(__name__)
            logger.exception(f"Error rejecting return {pk}: {str(e)}")
            return Response(
                {'detail': f'Error rejecting return: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=True, methods=['post'])
    def complete(self, request, pk=None):
        """
        Mark return as completed after refund is processed.
        
        Body: {}
        Returns: Updated return with status=COMPLETED
        """
        return_obj = self.get_object()
        
        if return_obj.status != 'APPROVED':
            return Response(
                {'detail': f'Can only complete approved returns (current status: {return_obj.status})'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            result = ReturnProcessor.complete_return(return_obj, request.user)
            return_obj.refresh_from_db()
            serializer = self.get_serializer(return_obj)
            return Response(
                {
                    'detail': 'Return completed successfully',
                    'processing': result,
                    'data': serializer.data
                },
                status=status.HTTP_200_OK
            )
        except ReturnProcessingError as e:
            logger = logging.getLogger(__name__)
            logger.exception(f"Return completion error for {pk}: {str(e)}")
            return Response(
                {'detail': f'Error: {str(e)}'},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            logger = logging.getLogger(__name__)
            logger.exception(f"Error completing return {pk}: {str(e)}")
            return Response(
                {'detail': f'Error completing return: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=False, methods=['get'])
    def by_type(self, request):
        """
        Get returns grouped by type.
        
        Query params: type (SALES|PURCHASE)
        Returns: List of returns filtered by type
        """
        return_type = request.query_params.get('type', None)
        
        if not return_type or return_type not in ['SALES', 'PURCHASE']:
            return Response(
                {'detail': 'Invalid or missing type parameter. Must be SALES or PURCHASE'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        queryset = self.get_queryset().filter(return_type=return_type)
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)


class ReturnItemViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing individual items in product returns.
    
    Note: ReturnItems are typically managed through the Return serializer's
    nested items field. This ViewSet provides direct access if needed.
    """
    queryset = ReturnItem.objects.all()
    serializer_class = ReturnItemSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['return_record', 'product']
    search_fields = ['product__name', 'product__sku']
    ordering = ['-return_record__created_at']



def invoice_queryset():
    return Invoice.objects.select_related(
        'customer', 'branch', 'pos_terminal', 'sale', 'created_by',
        'approved_by', 'rejected_by', 'cancelled_by', 'source_quotation',
    ).prefetch_related('items__product')


class InvoiceViewSet(BaseViewSet):
    """Invoices awaiting (or past) approval.

    An invoice is not a sale. ``approve`` is the only action that creates a
    confirmed sale and deducts stock; it is restricted to roles holding the
    ``invoice_approval`` permission and enforced here, not only in the UI.
    """
    serializer_class = InvoiceSerializer
    http_method_names = ['get', 'post', 'patch', 'head', 'options']

    def get_queryset(self):
        queryset = branch_scoped_queryset(invoice_queryset(), self.request.user)
        params = self.request.query_params

        status_filter = params.get('status')
        if status_filter:
            statuses = [s.strip().upper() for s in status_filter.split(',') if s.strip()]
            queryset = queryset.filter(status__in=statuses)
        if params.get('customer'):
            queryset = queryset.filter(customer_id=params['customer'])
        if params.get('branch'):
            queryset = queryset.filter(branch_id=params['branch'])
        search = params.get('search')
        if search:
            queryset = queryset.filter(
                Q(reference__icontains=search)
                | Q(customer__name__icontains=search)
                | Q(sale__reference__icontains=search)
            )
        if params.get('start_date'):
            queryset = queryset.filter(created_at__date__gte=params['start_date'])
        if params.get('end_date'):
            queryset = queryset.filter(created_at__date__lte=params['end_date'])
        return queryset

    @staticmethod
    def _error(exc):
        return Response(exc.as_payload(), status=exc.status_code)

    def _respond(self, invoice, extra=None, http_status=status.HTTP_200_OK):
        invoice = invoice_queryset().get(pk=invoice.pk)
        data = self.get_serializer(invoice).data
        if extra:
            data = {**extra, 'invoice': data}
        return Response(data, status=http_status)

    def _parse_items(self, raw):
        if not isinstance(raw, list) or not raw:
            raise ValidationError({'items': ['At least one item is required']})
        serializer = InvoiceItemInputSerializer(
            data=raw, many=True, context={'request': self.request},
        )
        serializer.is_valid(raise_exception=True)
        return [dict(item) for item in serializer.validated_data]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        items = self._parse_items(request.data.get('items'))
        data = serializer.validated_data
        branch = getattr(request.user, 'branch', None)
        pos_terminal = data.get('pos_terminal')
        if pos_terminal and branch and pos_terminal.branch_id != branch.id:
            raise ValidationError({'pos_terminal': 'Selected POS terminal does not belong to your branch.'})
        submit = request.data.get('submit', True)
        submit = submit if isinstance(submit, bool) else str(submit).lower() != 'false'
        try:
            invoice = invoicing.create_invoice(
                customer=data['customer'], items=items, user=request.user,
                branch=branch, pos_terminal=pos_terminal,
                notes=data.get('notes', ''), submit=submit,
            )
        except invoicing.InvoiceWorkflowError as exc:
            return self._error(exc)
        return self._respond(invoice, http_status=status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        invoice = self.get_object()
        items = self._parse_items(request.data['items']) if 'items' in request.data else None
        customer = None
        if 'customer' in request.data:
            serializer = self.get_serializer(invoice, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            customer = serializer.validated_data.get('customer')
        try:
            invoice = invoicing.update_invoice(
                invoice.pk, request.user, customer=customer, items=items,
                notes=request.data.get('notes'),
            )
        except invoicing.InvoiceWorkflowError as exc:
            return self._error(exc)
        return self._respond(invoice)

    @action(detail=True, methods=['post'])
    def submit(self, request, pk=None):
        invoice = self.get_object()
        try:
            invoice = invoicing.submit_invoice(invoice.pk, request.user)
        except invoicing.InvoiceWorkflowError as exc:
            return self._error(exc)
        return self._respond(invoice)

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        """Approve: create exactly one confirmed sale and deduct stock once.

        Repeats return the existing sale (HTTP 200, ``created: false``).
        """
        invoice = self.get_object()  # branch scoping
        try:
            invoice, sale, created = invoicing.approve_invoice(invoice.pk, request.user)
        except invoicing.InvoiceWorkflowError as exc:
            return self._error(exc)
        message = (
            f'Invoice {invoice.reference} approved. Sale {sale.reference} confirmed.'
            if created else
            f'Invoice {invoice.reference} was already approved as sale {sale.reference}.'
        )
        return self._respond(
            invoice,
            extra={'created': created, 'detail': message, 'sale_id': sale.pk, 'sale_reference': sale.reference},
            http_status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        invoice = self.get_object()
        try:
            invoice = invoicing.reject_invoice(invoice.pk, request.user, request.data.get('reason'))
        except invoicing.InvoiceWorkflowError as exc:
            return self._error(exc)
        return self._respond(invoice)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        invoice = self.get_object()
        try:
            invoice = invoicing.cancel_invoice(invoice.pk, request.user, request.data.get('reason', ''))
        except invoicing.InvoiceWorkflowError as exc:
            return self._error(exc)
        return self._respond(invoice)

    @action(detail=True, methods=['post'], url_path='mark-paid')
    def mark_paid(self, request, pk=None):
        invoice = self.get_object()
        if not invoice.sale_id:
            return Response(
                {'code': 'sale_not_confirmed', 'detail': 'Only approved invoices can be marked as paid.'},
                status=status.HTTP_409_CONFLICT,
            )
        try:
            invoicing.mark_sale_paid(invoice.sale_id, request.user)
        except invoicing.InvoiceWorkflowError as exc:
            return self._error(exc)
        return self._respond(invoice)

    @action(detail=True, methods=['get'])
    def history(self, request, pk=None):
        invoice = self.get_object()
        entries = invoice.history.select_related('actor', 'sale')
        return Response(InvoiceStatusHistorySerializer(entries, many=True).data)
