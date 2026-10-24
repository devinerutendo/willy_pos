from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from Accounts.models import User
from Core.models import Branch
from Inventory.models import Category, Location, Product, Stock, StockAdjustment, StockTransaction
from Inventory.serializers import ProductSerializer


class StockAdjustmentTests(TestCase):
	def setUp(self):
		self.user = User.objects.create_user(
			username='manager',
			email='manager@example.com',
			password='secret123',
			first_name='Branch',
			last_name='Manager',
			role=User.MANAGER,
		)
		self.branch = Branch.objects.create(
			name='Main Branch',
			code='MAIN',
			created_by=self.user,
			updated_by=self.user,
		)
		self.user.branch = self.branch
		self.user.save(update_fields=['branch'])
		self.category = Category.objects.create(
			name='Beverages',
			description='Drinks',
			created_by=self.user,
			updated_by=self.user,
		)
		self.product = Product.objects.create(
			name='Orange Juice',
			category=self.category,
			cost_price=Decimal('5.00'),
			selling_price=Decimal('7.50'),
			reorder_level=2,
			barcode='1234567890',
			created_by=self.user,
			updated_by=self.user,
		)
		self.location = Location.objects.create(
			branch=self.branch,
			name='Front Shelf',
			address='Aisle 1',
			created_by=self.user,
			updated_by=self.user,
		)
		self.stock = Stock.objects.create(
			product=self.product,
			location=self.location,
			quantity=10,
			reserved_quantity=0,
		)

	def test_apply_adjustment_updates_stock_and_writes_transaction(self):
		adjustment = StockAdjustment.apply_adjustment(
			stock=self.stock,
			new_quantity=14,
			user=self.user,
			reason='Cycle count correction',
			reference='ADJ-001',
			notes='Found extra units during recount.',
		)

		self.stock.refresh_from_db()
		transaction = StockTransaction.objects.get(reference='ADJ-001')

		self.assertEqual(self.stock.quantity, 14)
		self.assertEqual(adjustment.quantity_before, 10)
		self.assertEqual(adjustment.quantity_after, 14)
		self.assertEqual(adjustment.quantity_delta, 4)
		self.assertEqual(transaction.transaction_type, StockTransaction.ADJUSTMENT)
		self.assertEqual(transaction.quantity, 4)
		self.assertEqual(transaction.branch, self.branch)

	def test_barcode_lookup_and_sync_summary_endpoints(self):
		self.client.force_login(self.user)

		barcode_url = reverse('product-by-barcode')
		response = self.client.get(barcode_url, {'barcode': '1234567890'})
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.data['id'], self.product.id)
		self.assertEqual(response.data['barcode'], '1234567890')

		sync_url = reverse('product-sync')
		response = self.client.get(sync_url, {'updated_after': '2000-01-01T00:00:00Z'})
		self.assertEqual(response.status_code, 200)
		self.assertIn('products', response.data)
		self.assertIn('stock_transactions', response.data)
		self.assertGreaterEqual(response.data['count_products'], 1)

	def test_barcode_uniqueness(self):
		from django.db import IntegrityError
		with self.assertRaises(IntegrityError):
			Product.objects.create(
				name='Another Juice',
				category=self.category,
				cost_price=Decimal('5.00'),
				selling_price=Decimal('7.50'),
				barcode='1234567890', # Same as self.product
				created_by=self.user,
				updated_by=self.user,
			)

	def test_sku_generation(self):
		new_product = Product.objects.create(
			name='Apple Juice',
			category=self.category,
			cost_price=Decimal('4.00'),
			selling_price=Decimal('6.50'),
			barcode='0987654321',
			created_by=self.user,
			updated_by=self.user,
		)
		self.assertTrue(new_product.sku.startswith('BEV-'))
		
		another_product = Product.objects.create(
			name='Grape Juice',
			category=self.category,
			cost_price=Decimal('4.00'),
			selling_price=Decimal('6.50'),
			barcode='1122334455',
			created_by=self.user,
			updated_by=self.user,
		)
		self.assertNotEqual(new_product.sku, another_product.sku)
		self.assertTrue(another_product.sku.startswith('BEV-'))

	def test_blank_bundle_fields_are_treated_as_unset(self):
		category = Category.objects.create(
			name='Office Supplies',
			description='Stationery',
			created_by=self.user,
			updated_by=self.user,
		)

		serializer = ProductSerializer(data={
			'name': 'Desk Organizer',
			'category_id': category.id,
			'cost_price': '20.00',
			'selling_price': '35.00',
			'reorder_level': 3,
			'barcode': '',
			'bundle_quantity': '',
			'bundle_price': '',
		})

		self.assertTrue(serializer.is_valid(), serializer.errors)
		product = serializer.save(created_by=self.user, updated_by=self.user)
		self.assertIsNone(product.bundle_quantity)
		self.assertIsNone(product.bundle_price)

	def test_stock_reconciliation(self):
		# Reconcile StockTransaction deltas with Stock quantity
		new_stock = Stock.objects.create(
			product=self.product,
			location=self.location,
			quantity=0,
			reserved_quantity=0,
		)
		StockAdjustment.apply_adjustment(
			stock=new_stock,
			new_quantity=50,
			user=self.user,
			reason='Initial stock',
			reference='REC-001'
		)
		StockAdjustment.apply_adjustment(
			stock=new_stock,
			new_quantity=45,
			user=self.user,
			reason='Damage',
			reference='REC-002'
		)
		
		# Sum of transactions should equal the current quantity
		total_delta = sum(t.quantity for t in StockTransaction.objects.filter(stock=new_stock))
		new_stock.refresh_from_db()
		self.assertEqual(new_stock.quantity, total_delta)


class StockAdjustmentOptionalLocationTests(TestCase):
	"""Location is optional unless several branches exist and the user is not branch-bound."""

	def setUp(self):
		from types import SimpleNamespace

		self.SimpleNamespace = SimpleNamespace
		self.admin = User.objects.create_user(
			username='admin1', email='admin1@example.com', password='secret123',
			first_name='A', last_name='B', role=User.ADMIN,
		)
		self.branch = Branch.objects.create(
			name='Main Branch', code='MAIN', created_by=self.admin, updated_by=self.admin,
		)
		category = Category.objects.create(
			name='Beverages', description='Drinks', created_by=self.admin, updated_by=self.admin,
		)
		self.product = Product.objects.create(
			name='Cola', category=category, cost_price=Decimal('1.00'),
			selling_price=Decimal('2.00'), reorder_level=1, created_by=self.admin, updated_by=self.admin,
		)

	def _serializer(self, user, **extra):
		from Inventory.serializers import StockAdjustmentSerializer

		request = self.SimpleNamespace(user=user)
		payload = {'product': self.product.id, 'quantity_after': 5, 'reason': 'Count'}
		payload.update(extra)
		return StockAdjustmentSerializer(data=payload, context={'request': request})

	def test_location_not_required_with_single_branch(self):
		serializer = self._serializer(self.admin)
		self.assertTrue(serializer.is_valid(), serializer.errors)
		adjustment = serializer.save(created_by=self.admin, updated_by=self.admin)
		self.assertEqual(adjustment.location.branch, self.branch)
		self.assertEqual(Stock.objects.get(product=self.product).quantity, 5)

	def test_location_required_with_multiple_branches_for_unbound_user(self):
		Branch.objects.create(name='Second', code='SEC', created_by=self.admin, updated_by=self.admin)
		serializer = self._serializer(self.admin)
		self.assertFalse(serializer.is_valid())
		self.assertIn('location', serializer.errors)

	def test_branch_bound_user_does_not_need_location_with_multiple_branches(self):
		other = Branch.objects.create(name='Second', code='SEC', created_by=self.admin, updated_by=self.admin)
		self.admin.branch = other
		self.admin.save(update_fields=['branch'])
		serializer = self._serializer(self.admin)
		self.assertTrue(serializer.is_valid(), serializer.errors)
		adjustment = serializer.save(created_by=self.admin, updated_by=self.admin)
		self.assertEqual(adjustment.location.branch, other)

	def test_default_location_prefers_location_holding_stock(self):
		empty = Location.objects.create(branch=self.branch, name='A-Empty', created_by=self.admin, updated_by=self.admin)
		stocked = Location.objects.create(branch=self.branch, name='B-Stocked', created_by=self.admin, updated_by=self.admin)
		Stock.objects.create(product=self.product, location=stocked, quantity=3)
		self.assertEqual(Location.resolve_default(branch=self.branch, product=self.product), stocked)
		self.assertNotEqual(empty, stocked)


class CategoryApiTests(TestCase):
	"""Category endpoint: product counts, duplicate protection and safe deletion."""

	def setUp(self):
		from rest_framework.test import APIClient

		self.admin = User.objects.create_user(
			username='catadmin', email='catadmin@example.com', password='secret123',
			first_name='C', last_name='A', role=User.ADMIN,
		)
		self.client = APIClient()
		self.client.force_authenticate(self.admin)
		self.url = '/api/inventory/categories/'
		self.category = Category.objects.create(
			name='Beverages', created_by=self.admin, updated_by=self.admin,
		)
		Product.objects.create(
			name='Cola', category=self.category, cost_price=Decimal('1.00'),
			selling_price=Decimal('2.00'), reorder_level=1, created_by=self.admin, updated_by=self.admin,
		)

	def _results(self, response):
		data = response.json()
		return data['results'] if isinstance(data, dict) and 'results' in data else data

	def test_list_includes_product_count_and_search(self):
		Category.objects.create(name='Snacks', created_by=self.admin, updated_by=self.admin)
		response = self.client.get(self.url)
		self.assertEqual(response.status_code, 200)
		counts = {c['name']: c['product_count'] for c in self._results(response)}
		self.assertEqual(counts, {'Beverages': 1, 'Snacks': 0})

		response = self.client.get(self.url, {'search': 'snack'})
		self.assertEqual([c['name'] for c in self._results(response)], ['Snacks'])

	def test_duplicate_name_is_rejected_case_insensitively(self):
		response = self.client.post(self.url, {'name': ' beverages '}, format='json')
		self.assertEqual(response.status_code, 400)
		self.assertIn('name', response.json())

	def test_rename_to_own_name_is_allowed(self):
		response = self.client.patch(
			f'{self.url}{self.category.id}/', {'name': 'BEVERAGES'}, format='json',
		)
		self.assertEqual(response.status_code, 200, response.content)

	def test_delete_blocked_while_products_assigned(self):
		response = self.client.delete(f'{self.url}{self.category.id}/')
		self.assertEqual(response.status_code, 409)
		self.assertIn('1 product', response.json()['detail'])
		self.assertTrue(Category.objects.filter(pk=self.category.pk).exists())

	def test_empty_category_can_be_deleted(self):
		empty = Category.objects.create(name='Empty', created_by=self.admin, updated_by=self.admin)
		response = self.client.delete(f'{self.url}{empty.id}/')
		self.assertEqual(response.status_code, 204)
		self.assertFalse(Category.objects.filter(pk=empty.pk).exists())
