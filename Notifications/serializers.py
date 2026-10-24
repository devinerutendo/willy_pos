from rest_framework import serializers
from .models import Notification, NotificationPreference, NotificationLog, NotificationType
from .services import PREFERENCE_CATEGORIES, sms_delivery_available


def _boolean_object_serializer(field_names):
    """Build a serializer of optional booleans (omitted keys keep their value)."""
    return type(
        'BooleanMap',
        (serializers.Serializer,),
        {name: serializers.BooleanField(required=False) for name in field_names},
    )


CategoryStateSerializer = _boolean_object_serializer(PREFERENCE_CATEGORIES.keys())
ChannelStateSerializer = _boolean_object_serializer(['email', 'sms'])


class NotificationSettingsInputSerializer(serializers.Serializer):
    categories = CategoryStateSerializer(required=False)
    channels = ChannelStateSerializer(required=False)

    def validate_channels(self, value):
        if value.get('sms') and not sms_delivery_available():
            raise serializers.ValidationError({'sms': 'SMS delivery is not available on this server.'})
        return value


class NotificationSerializer(serializers.ModelSerializer):
    notification_type_display = serializers.CharField(source='get_notification_type_display', read_only=True)
    level_display = serializers.CharField(source='get_level_display', read_only=True)

    class Meta:
        model = Notification
        fields = [
            'id', 'user', 'notification_type', 'notification_type_display', 'level',
            'level_display', 'title', 'message', 'payload', 'is_read',
            'created_at', 'resolved_at', 'related_object_type', 'related_object_id',
        ]
        read_only_fields = [
            'id', 'user', 'notification_type_display', 'level_display',
            'created_at', 'resolved_at',
        ]


class NotificationPreferenceSerializer(serializers.ModelSerializer):
    notification_type_display = serializers.CharField(source='get_notification_type_display', read_only=True)

    class Meta:
        model = NotificationPreference
        fields = [
            'id', 'user', 'notification_type', 'notification_type_display',
            'channels', 'enabled', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'user', 'notification_type_display', 'created_at', 'updated_at']

    def validate_channels(self, value):
        valid_channels = {'in_app', 'email', 'sms'}
        if not isinstance(value, list):
            raise serializers.ValidationError('Channels must be a list of strings.')
        invalid = [channel for channel in value if channel not in valid_channels]
        if invalid:
            raise serializers.ValidationError(f'Invalid channels: {invalid}')
        return value

    def create(self, validated_data):
        validated_data['user'] = self.context['request'].user
        return super().create(validated_data)

    def update(self, instance, validated_data):
        validated_data.pop('user', None)
        return super().update(instance, validated_data)


class NotificationLogSerializer(serializers.ModelSerializer):
    notification = NotificationSerializer(read_only=True)

    class Meta:
        model = NotificationLog
        fields = ['id', 'notification', 'channel', 'status', 'attempted_at', 'response']
        read_only_fields = fields
