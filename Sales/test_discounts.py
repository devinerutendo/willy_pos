from decimal import Decimal

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIRequestFactory, force_authenticate

from Accounts.models import User
from Core.models import Branch, POSTerminal
from Inventory.models import Category, Location, Product, Stock
from Sales.models import Sale, Quotation, Customer
from Sales.views import SaleViewSet, QuotationViewSet


class SaleDiscountTests(TestCase):
    """Test discount functionality for sales items."""
    
    def setUp(self):
        self.factory = APIRequestFactory()
        # Create admin user
        self.admin = User.objects.create_user(
            username='admin',
            email='admin@example.com',
            password='secret123',
            first_name='Admin',
            last_name='User',
            role=User.ADMIN,
            is_superuser=True,
        )
        # Create regular sales user
        self.cashier = User.objects.create_user(
            username='cashier',
            email='cashier@example.com',
            password='secret123',
            first_name='Cash',
            last_name='ier',
            role=User.SALES,
        )
        self.branch = Branch.objects.create(
            name='Downtown',
            code='DTN',
            created_by=self.admin,
            updated_by=self.admin,
        )
        self.admin.branch = self.branch
        self.admin.save(update_fields=['branch'])
        self.cashier.branch = self.branch
        self.cashier.save(update_fields=['branch'])
        
        self.terminal = POSTerminal.objects.create(
            branch=self.branch,
            name='Front Counter',
            code='POS-001',
            created_by=self.admin,
            updated_by=self.admin,
        )
        self.category = Category.objects.create(
            name='Snacks',
            description='Shelf goods',
            created_by=self.admin,
            updated_by=self.admin,
        )
        self.location = Location.objects.create(
            name='Shelf A',
            created_by=self.admin,
            updated_by=self.admin,
        )
        self.product = Product.objects.create(
            name='Potato Chips',
            category=self.category,
            cost_price=Decimal('2.00'),
            selling_price=Decimal('10.00'),
            created_by=self.admin,
            updated_by=self.admin,
        )
        Stock.objects.create(
            product=self.product,
            location=self.location,
            quantity=100,
        )
    
    def test_admin_can_apply_percentage_discount(self):
        """Test that admin can apply percentage discount to sale items."""
        request = self.factory.post(
            '/api/sales/sales/',
            {
                'branch': self.branch.id,
                'pos_terminal': self.terminal.id,
                'payment_method': 'CASH',
                'items': [
                    {
                        'product': self.product.id,
                        'quantity': 2,
                        'unit_price': '10.00',
                        'discount_percentage': '20',  # 20% discount
                    }
                ],
            },
            format='json',
        )
        force_authenticate(request, user=self.admin)
        
        response = SaleViewSet.as_view({'post': 'create'})(request)
        
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        sale = Sale.objects.get(id=response.data['id'])
        sale_item = sale.items.first()
        
        # Original: 2 * 10 = 20, with 20% discount: 20 * 0.8 = 16
        self.assertEqual(sale_item.discount_percentage, Decimal('20'))
        self.assertEqual(sale_item.effective_unit_price, Decimal('8.00'))
        self.assertEqual(sale_item.line_total, Decimal('16.00'))
    
    def test_admin_can_apply_fixed_discount(self):
        """Test that admin can apply fixed discount to sale items."""
        request = self.factory.post(
            '/api/sales/sales/',
            {
                'branch': self.branch.id,
                'pos_terminal': self.terminal.id,
                'payment_method': 'CASH',
                'items': [
                    {
                        'product': self.product.id,
                        'quantity': 2,
                        'unit_price': '10.00',
                        'discount_amount': '3.00',  # Fixed $3 discount
                    }
                ],
            },
            format='json',
        )
        force_authenticate(request, user=self.admin)
        
        response = SaleViewSet.as_view({'post': 'create'})(request)
        
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        sale = Sale.objects.get(id=response.data['id'])
        sale_item = sale.items.first()
        
        # Unit price: 10 - 3 = 7 per unit, total: 2 * 7 = 14
        self.assertEqual(sale_item.discount_amount, Decimal('3.00'))
        self.assertEqual(sale_item.effective_unit_price, Decimal('7.00'))
        self.assertEqual(sale_item.line_total, Decimal('14.00'))
    
    def test_admin_can_apply_combined_discount(self):
        """Test that admin can apply both percentage and fixed discount."""
        request = self.factory.post(
            '/api/sales/sales/',
            {
                'branch': self.branch.id,
                'pos_terminal': self.terminal.id,
                'payment_method': 'CASH',
                'items': [
                    {
                        'product': self.product.id,
                        'quantity': 2,
                        'unit_price': '10.00',
                        'discount_percentage': '20',  # 20% = $2 off
                        'discount_amount': '1.00',  # Plus $1 fixed
                    }
                ],
            },
            format='json',
        )
        force_authenticate(request, user=self.admin)
        
        response = SaleViewSet.as_view({'post': 'create'})(request)
        
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        sale = Sale.objects.get(id=response.data['id'])
        sale_item = sale.items.first()
        
        # Percentage first: 10 * (1 - 0.20) = 8, then fixed: 8 - 1 = 7 per unit, total: 2 * 7 = 14
        self.assertEqual(sale_item.discount_percentage, Decimal('20'))
        self.assertEqual(sale_item.discount_amount, Decimal('1.00'))
        self.assertEqual(sale_item.effective_unit_price, Decimal('7.00'))
        self.assertEqual(sale_item.line_total, Decimal('14.00'))
    
    def test_non_admin_cannot_apply_discount(self):
        """Test that non-admin users cannot apply discounts."""
        request = self.factory.post(
            '/api/sales/sales/',
            {
                'branch': self.branch.id,
                'pos_terminal': self.terminal.id,
                'payment_method': 'CASH',
                'items': [
                    {
                        'product': self.product.id,
                        'quantity': 2,
                        'unit_price': '10.00',
                        'discount_percentage': '20',
                    }
                ],
            },
            format='json',
        )
        force_authenticate(request, user=self.cashier)
        
        response = SaleViewSet.as_view({'post': 'create'})(request)
        
        # Should fail with validation error
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Only admins can apply discounts', str(response.data))


class QuotationDiscountTests(TestCase):
    """Test discount functionality for quotation items."""
    
    def setUp(self):
        self.factory = APIRequestFactory()
        # Create admin user
        self.admin = User.objects.create_user(
            username='admin',
            email='admin@example.com',
            password='secret123',
            first_name='Admin',
            last_name='User',
            role=User.ADMIN,
            is_superuser=True,
        )
        # Create regular sales user
        self.sales_rep = User.objects.create_user(
            username='sales',
            email='sales@example.com',
            password='secret123',
            first_name='Sales',
            last_name='Rep',
            role=User.SALES,
        )
        self.branch = Branch.objects.create(
            name='Downtown',
            code='DTN',
            created_by=self.admin,
            updated_by=self.admin,
        )
        self.admin.branch = self.branch
        self.admin.save(update_fields=['branch'])
        self.sales_rep.branch = self.branch
        self.sales_rep.save(update_fields=['branch'])
        
        self.terminal = POSTerminal.objects.create(
            branch=self.branch,
            name='Front Counter',
            code='POS-001',
            created_by=self.admin,
            updated_by=self.admin,
        )
        self.category = Category.objects.create(
            name='Electronics',
            description='Tech products',
            created_by=self.admin,
            updated_by=self.admin,
        )
        self.product = Product.objects.create(
            name='USB Cable',
            category=self.category,
            cost_price=Decimal('1.00'),
            selling_price=Decimal('5.00'),
            created_by=self.admin,
            updated_by=self.admin,
        )
        # Create a test customer
        self.customer = Customer.objects.create(
            name='Test Customer',
            phone='1234567890',
            is_active=True,
            created_by=self.admin,
            updated_by=self.admin,
        )
    
    def test_admin_can_create_quotation_with_discount(self):
        """Test that admin can create quotation with discount."""
        request = self.factory.post(
            '/api/sales/quotations/',
            {
                'customer': self.customer.id,
                'branch': self.branch.id,
                'pos_terminal': self.terminal.id,
                'valid_until': '2026-12-31',
                'items': [
                    {
                        'product': self.product.id,
                        'quantity': 10,
                        'unit_price': '5.00',
                        'discount_percentage': '15',
                    }
                ],
            },
            format='json',
        )
        force_authenticate(request, user=self.admin)
        
        response = QuotationViewSet.as_view({'post': 'create'})(request)
        
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        quotation = Quotation.objects.get(id=response.data['id'])
        quotation_item = quotation.items.first()
        
        # 5.00 * (1 - 0.15) = 4.25 per unit
        self.assertEqual(quotation_item.discount_percentage, Decimal('15'))
        self.assertEqual(quotation_item.effective_unit_price, Decimal('4.25'))
        self.assertEqual(quotation_item.line_total, Decimal('42.50'))
    
    def test_non_admin_cannot_create_quotation_with_discount(self):
        """Test that non-admin cannot create quotation with discount."""
        request = self.factory.post(
            '/api/sales/quotations/',
            {
                'customer': self.customer.id,
                'branch': self.branch.id,
                'pos_terminal': self.terminal.id,
                'valid_until': '2026-12-31',
                'items': [
                    {
                        'product': self.product.id,
                        'quantity': 10,
                        'unit_price': '5.00',
                        'discount_percentage': '15',
                    }
                ],
            },
            format='json',
        )
        force_authenticate(request, user=self.sales_rep)
        
        response = QuotationViewSet.as_view({'post': 'create'})(request)
        
        # Should fail with validation error
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Only admins can apply discounts', str(response.data))
