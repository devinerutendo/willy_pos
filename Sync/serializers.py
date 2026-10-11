from rest_framework import serializers
from .models import SyncOperation

class SyncOperationSerializer(serializers.ModelSerializer):
    class Meta:
        model = SyncOperation
        fields = '__all__'
        read_only_fields = ['status', 'attempt_count', 'error_message', 'created_at', 'last_attempt_at', 'synced_at']
