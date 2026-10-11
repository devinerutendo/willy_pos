from rest_framework import serializers

from .models import AuditLog, Branch, POSTerminal


class BranchSerializer(serializers.ModelSerializer):
    created_by = serializers.PrimaryKeyRelatedField(read_only=True)
    updated_by = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = Branch
        fields = [
            'id', 'name', 'code', 'address', 'phone', 'email', 'is_active',
            'created_at', 'updated_at', 'created_by', 'updated_by'
        ]
        read_only_fields = ['created_at', 'updated_at', 'created_by', 'updated_by']


class POSTerminalSerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    assigned_user_name = serializers.CharField(source='assigned_user.full_name', read_only=True)
    created_by = serializers.PrimaryKeyRelatedField(read_only=True)
    updated_by = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = POSTerminal
        fields = [
            'id', 'name', 'code', 'branch', 'branch_name', 'description', 'assigned_user',
            'assigned_user_name', 'is_active', 'last_seen_at', 'created_at', 'updated_at',
            'created_by', 'updated_by'
        ]
        read_only_fields = ['created_at', 'updated_at', 'created_by', 'updated_by', 'last_seen_at']


class AuditLogSerializer(serializers.ModelSerializer):
    actor_name = serializers.CharField(source='actor.full_name', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True)

    class Meta:
        model = AuditLog
        fields = [
            'id', 'action', 'model_label', 'object_pk', 'object_repr', 'changes',
            'actor', 'actor_name', 'branch', 'branch_name', 'created_at'
        ]
        read_only_fields = fields