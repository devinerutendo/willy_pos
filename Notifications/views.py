from rest_framework import viewsets, permissions, status
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from .models import Notification, NotificationPreference, NotificationLog
from .serializers import NotificationSerializer, NotificationPreferenceSerializer, NotificationLogSerializer


class NotificationViewSet(viewsets.ModelViewSet):
    queryset = Notification.objects.all().select_related('user')
    serializer_class = NotificationSerializer
    permission_classes = [permissions.IsAuthenticated]
    http_method_names = ['get', 'patch', 'head', 'options']

    def get_queryset(self):
        user = self.request.user
        if user.is_authenticated and user.is_manager:
            return Notification.objects.all()
        return Notification.objects.filter(user=user)

    def partial_update(self, request, *args, **kwargs):
        notification = self.get_object()
        serializer = self.get_serializer(notification, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class NotificationPreferenceViewSet(viewsets.ModelViewSet):
    queryset = NotificationPreference.objects.all()
    serializer_class = NotificationPreferenceSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        return NotificationPreference.objects.filter(user=user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class NotificationLogViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = NotificationLog.objects.all().select_related('notification')
    serializer_class = NotificationLogSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        if user.is_authenticated and user.is_manager:
            return NotificationLog.objects.all()
        return NotificationLog.objects.filter(notification__user=user)
