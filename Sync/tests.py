from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIRequestFactory, force_authenticate
from Accounts.models import User
from Core.models import Branch, POSTerminal
from Sync.views import SyncPullView, SyncPushView, SyncStatusView

class SyncTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = User.objects.create_user(
            username='syncuser',
            email='syncuser@example.com',
            password='secret123',
            first_name='Sync',
            last_name='User',
            role=User.MANAGER,
        )
        self.branch = Branch.objects.create(
            name='Sync Branch',
            code='SYNC',
            created_by=self.user,
            updated_by=self.user,
        )
        self.user.branch = self.branch
        self.user.save(update_fields=['branch'])
        self.terminal = POSTerminal.objects.create(
            branch=self.branch,
            name='Sync Terminal',
            code='POS-SYNC',
            created_by=self.user,
            updated_by=self.user,
        )

    def test_sync_status(self):
        request = self.factory.get('/api/sync/status/?device_id=TEST-DEVICE')
        force_authenticate(request, user=self.user)
        response = SyncStatusView.as_view()(request)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('pending_operations', response.data)
        self.assertIn('failed_operations', response.data)
        self.assertIn('server_time', response.data)

    def test_sync_pull(self):
        request = self.factory.get('/api/sync/pull/')
        force_authenticate(request, user=self.user)
        response = SyncPullView.as_view()(request)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('products', response.data)
        self.assertIn('customers', response.data)

    def test_sync_push(self):
        import uuid
        op_id = str(uuid.uuid4())
        client_id = str(uuid.uuid4())
        payload = {
            "device_id": "TEST-DEVICE-01",
            "operations": [
                {
                    "operation_id": op_id,
                    "entity_type": "customer",
                    "action": "CREATE",
                    "payload": {
                        "name": "Sync Customer",
                        "email": "sync@example.com",
                        "client_id": client_id,
                        "phone": "00000"
                    }
                }
            ]
        }
        request = self.factory.post('/api/sync/push/', payload, format='json')
        force_authenticate(request, user=self.user)
        response = SyncPushView.as_view()(request)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('results', response.data)
        self.assertIn(op_id, response.data['results'])
        self.assertEqual(response.data['results'][op_id]['status'], 'success')
