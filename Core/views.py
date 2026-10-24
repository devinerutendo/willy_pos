import json
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse
from django.utils import timezone
from rest_framework import mixins, permissions, status, viewsets
from rest_framework.decorators import action, api_view
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from Core.access import branch_scoped_queryset
from Core.permissions import ScopePermission
from .models import AuditLog, Branch, CompanyProfile, POSTerminal
from .serializers import (
    AuditLogSerializer,
    BranchSerializer,
    CompanyProfileSerializer,
    POSTerminalSerializer,
)

logger = logging.getLogger(__name__)

# Only files created by the backup tooling are listed, downloadable and pruned.
# Anything else in the backups folder (schedule/event metadata, temporary
# restore uploads) is deliberately ignored.
BACKUP_FILE_PREFIXES = ('backup-', 'manual-backup-', 'auto-backup-', 'pre-restore-backup-')
MIN_RETAIN_COUNT = 1
MAX_RETAIN_COUNT = 365
MAX_RESTORE_UPLOAD_BYTES = 100 * 1024 * 1024
BACKUP_FREQUENCIES = {'hourly', 'daily', 'weekly', 'monthly'}


def _parse_bool(value, default=False):
    """Strictly parse booleans from JSON, form data or query strings."""
    if value is None or value == '':
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {'true', '1', 'yes', 'on'}:
        return True
    if text in {'false', '0', 'no', 'off'}:
        return False
    raise ValueError(f'Invalid boolean value: {value!r}')


class HealthCheckView(APIView):
    """Simple health check endpoint"""
    permission_classes = [permissions.AllowAny]
    
    def get(self, request):
        return Response({
            'status': 'ok',
            'service': 'inventory-management-api',
            'timestamp': timezone.now().isoformat()
        })


class AuditAwareViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated, ScopePermission]
    permission_scope = None

    def _get_save_kwargs(self, serializer, include_created=False):
        model = getattr(getattr(serializer, 'Meta', None), 'model', None)
        if not model:
            return {}

        field_names = {field.name for field in model._meta.get_fields()}
        kwargs = {}
        if include_created and 'created_by' in field_names:
            kwargs['created_by'] = self.request.user
        if 'updated_by' in field_names:
            kwargs['updated_by'] = self.request.user
        if 'branch' in field_names and 'branch' not in serializer.validated_data:
            user_branch = getattr(self.request.user, 'branch', None)
            if user_branch is not None:
                kwargs['branch'] = user_branch
        return kwargs

    def perform_create(self, serializer):
        serializer.save(**self._get_save_kwargs(serializer, include_created=True))

    def perform_update(self, serializer):
        serializer.save(**self._get_save_kwargs(serializer))


class BranchViewSet(AuditAwareViewSet):
    queryset = Branch.objects.all().order_by('name')
    serializer_class = BranchSerializer
    permission_scope = 'branches'


class POSTerminalViewSet(AuditAwareViewSet):
    serializer_class = POSTerminalSerializer
    permission_scope = 'pos'

    def get_queryset(self):
        queryset = POSTerminal.objects.select_related('branch', 'assigned_user').order_by('branch__name', 'name')
        queryset = branch_scoped_queryset(queryset, self.request.user)

        branch = self.request.query_params.get('branch')
        if branch:
            queryset = queryset.filter(branch_id=branch)

        return queryset


class CompanyProfileView(APIView):
    """
    Business identity (name, contact details, terms and logo) used on receipts
    and documents.

    GET   - any authenticated user (every till needs it to print receipts).
    PUT / PATCH - roles with write access to the ``company`` scope.
    The logo is uploaded as multipart ``logo`` and removed with ``remove_logo``.
    """

    permission_classes = [permissions.IsAuthenticated, ScopePermission]
    permission_scope = 'company'
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get(self, request):
        return Response(CompanyProfileSerializer(CompanyProfile.load()).data)

    def put(self, request):
        return self._update(request, partial=False)

    def patch(self, request):
        return self._update(request, partial=True)

    def _update(self, request, partial):
        with transaction.atomic():
            CompanyProfile.load()
            # Row lock so concurrent edits (and logo replacements) apply one at a time.
            profile = CompanyProfile.objects.select_for_update().get(pk=CompanyProfile.SINGLETON_PK)
            serializer = CompanyProfileSerializer(profile, data=request.data, partial=partial)
            serializer.is_valid(raise_exception=True)
            serializer.save(updated_by=request.user)
        return Response(serializer.data)


class AuditLogViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    serializer_class = AuditLogSerializer
    permission_classes = [permissions.IsAuthenticated, ScopePermission]
    permission_scope = 'audit'

    def get_queryset(self):
        queryset = AuditLog.objects.select_related('actor', 'branch').order_by('-created_at')
        queryset = branch_scoped_queryset(queryset, self.request.user)

        model_label = self.request.query_params.get('model')
        if model_label:
            queryset = queryset.filter(model_label=model_label)

        object_pk = self.request.query_params.get('object_pk')
        if object_pk:
            queryset = queryset.filter(object_pk=object_pk)

        action_filter = self.request.query_params.get('action')
        if action_filter:
            queryset = queryset.filter(action=action_filter.upper())

        search = (self.request.query_params.get('search') or '').strip()
        if search:
            queryset = queryset.filter(
                Q(object_repr__icontains=search)
                | Q(model_label__icontains=search)
                | Q(actor__first_name__icontains=search)
                | Q(actor__last_name__icontains=search)
                | Q(actor__email__icontains=search)
            )

        return queryset


class BackupViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticated]

    @property
    def backup_dir(self) -> Path:
        backup_dir = Path(settings.BASE_DIR) / 'backups'
        backup_dir.mkdir(parents=True, exist_ok=True)
        return backup_dir

    @property
    def schedule_file(self) -> Path:
        return self.backup_dir / 'backup_schedule.json'

    @property
    def events_file(self) -> Path:
        return self.backup_dir / 'backup_events.json'

    def _ensure_manager(self, request):
        user = request.user
        if not getattr(user, 'is_authenticated', False):
            return Response({'detail': 'Authentication required.'}, status=status.HTTP_401_UNAUTHORIZED)

        if getattr(user, 'is_superuser', False) or getattr(user, 'is_manager', False):
            return None

        return Response({'detail': 'Only administrators and managers can manage backups.'}, status=status.HTTP_403_FORBIDDEN)

    def _serialize_backup_file(self, path: Path) -> dict:
        stat = path.stat()
        return {
            'name': path.name,
            'size': stat.st_size,
            'created_at': datetime.fromtimestamp(stat.st_mtime, tz=timezone.get_current_timezone()).isoformat(),
        }

    def _is_backup_file(self, path: Path) -> bool:
        return (
            path.is_file()
            and path.suffix.lower() == '.json'
            and path.name.startswith(BACKUP_FILE_PREFIXES)
        )

    def _backup_files(self) -> list[Path]:
        files = [path for path in self.backup_dir.glob('*.json') if self._is_backup_file(path)]
        return sorted(files, key=lambda item: item.stat().st_mtime, reverse=True)

    def _list_backups(self) -> list[dict]:
        return [self._serialize_backup_file(path) for path in self._backup_files()]

    def _load_events(self) -> list[dict]:
        if not self.events_file.exists():
            return []

        try:
            with self.events_file.open('r', encoding='utf-8') as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return []

        if not isinstance(payload, list):
            return []

        return payload

    def _save_events(self, events: list[dict]):
        with self.events_file.open('w', encoding='utf-8') as handle:
            json.dump(events[:100], handle, indent=2)

    def _append_event(self, event_type: str, message: str, actor=None, file_name=None, outcome='SUCCESS'):
        events = self._load_events()
        entry = {
            'timestamp': timezone.localtime().isoformat(),
            'type': event_type,
            'message': message,
            'outcome': outcome,
            'actor': getattr(actor, 'email', None),
            'file': file_name,
        }
        events.insert(0, entry)
        self._save_events(events)

    def _default_schedule(self) -> dict:
        return {
            'enabled': False,
            'automatic': False,
            'frequency': 'daily',
            'time': '02:00',
            'retain_count': 14,
            'last_backup_at': None,
            'next_backup_at': None,
        }

    def _load_schedule(self) -> dict:
        if not self.schedule_file.exists():
            return self._default_schedule()

        try:
            with self.schedule_file.open('r', encoding='utf-8') as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return self._default_schedule()

        default = self._default_schedule()
        default.update(data)
        return default

    def _save_schedule(self, schedule: dict):
        with self.schedule_file.open('w', encoding='utf-8') as handle:
            json.dump(schedule, handle, indent=2)

    def _calculate_next_run(self, frequency: str, at_time: str, from_dt=None) -> datetime:
        current = from_dt or timezone.localtime()
        hour, minute = [int(piece) for piece in at_time.split(':')]
        candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)

        if frequency == 'hourly':
            candidate = current.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            return candidate

        if candidate <= current:
            if frequency == 'weekly':
                candidate += timedelta(days=7)
            elif frequency == 'monthly':
                candidate += timedelta(days=30)
            else:
                candidate += timedelta(days=1)

        return candidate

    def _cleanup_old_backups(self, retain_count: int):
        if retain_count <= 0:
            return

        for path in self._backup_files()[retain_count:]:
            path.unlink(missing_ok=True)

    def _create_backup(self, prefix: str = 'backup') -> str:
        filename = f"{prefix}-{timezone.localtime().strftime('%Y%m%d-%H%M%S')}.json"
        output_path = self.backup_dir / filename
        call_command('backup_system', output=str(output_path))
        return filename

    def _maybe_run_automatic_backup(self) -> str | None:
        schedule = self._load_schedule()
        if not (schedule.get('enabled') and schedule.get('automatic')):
            return None

        next_backup_at = schedule.get('next_backup_at')
        if not next_backup_at:
            return None

        try:
            due_at = datetime.fromisoformat(next_backup_at)
        except ValueError:
            return None

        now = timezone.localtime()
        if due_at > now:
            return None

        created_name = self._create_backup(prefix='auto-backup')
        self._append_event('AUTO_BACKUP', f'Automatic backup created: {created_name}', file_name=created_name)
        schedule['last_backup_at'] = now.isoformat()
        next_run = self._calculate_next_run(schedule.get('frequency', 'daily'), schedule.get('time', '02:00'), from_dt=now)
        schedule['next_backup_at'] = next_run.isoformat()
        self._save_schedule(schedule)
        self._cleanup_old_backups(int(schedule.get('retain_count') or 14))
        return created_name

    @action(detail=False, methods=['get'], url_path='status')
    def status(self, request):
        denied = self._ensure_manager(request)
        if denied:
            return denied

        auto_created = self._maybe_run_automatic_backup()
        schedule = self._load_schedule()
        backups = self._list_backups()
        events = self._load_events()[:20]
        return Response({'schedule': schedule, 'backups': backups, 'events': events, 'automatic_backup_created': auto_created})

    @action(detail=False, methods=['post'], url_path='backup_now')
    def backup_now(self, request):
        denied = self._ensure_manager(request)
        if denied:
            return denied

        try:
            filename = self._create_backup(prefix='manual-backup')
        except Exception as exc:
            logger.exception('Manual backup failed')
            self._append_event('MANUAL_BACKUP', f'Manual backup failed: {exc}', actor=request.user, outcome='FAILED')
            return Response({'detail': 'Backup failed. Check the server logs for details.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        self._append_event('MANUAL_BACKUP', f'Manual backup created: {filename}', actor=request.user, file_name=filename)
        schedule = self._load_schedule()
        schedule['last_backup_at'] = timezone.localtime().isoformat()
        if schedule.get('enabled') and schedule.get('automatic'):
            schedule['next_backup_at'] = self._calculate_next_run(schedule.get('frequency', 'daily'), schedule.get('time', '02:00')).isoformat()
        self._save_schedule(schedule)
        self._cleanup_old_backups(int(schedule.get('retain_count') or 14))

        return Response({'detail': 'Backup completed successfully.', 'file': filename}, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['post'], url_path='schedule')
    def schedule(self, request):
        denied = self._ensure_manager(request)
        if denied:
            return denied

        try:
            enabled = _parse_bool(request.data.get('enabled'), default=False)
            automatic = _parse_bool(request.data.get('automatic'), default=False)
        except ValueError:
            return Response({'detail': 'enabled and automatic must be true or false.'}, status=status.HTTP_400_BAD_REQUEST)

        frequency = str(request.data.get('frequency') or 'daily').lower()
        at_time = str(request.data.get('time') or '02:00')

        raw_retain = request.data.get('retain_count')
        try:
            retain_count = 14 if raw_retain in (None, '') else int(raw_retain)
        except (TypeError, ValueError):
            return Response({'detail': 'retain_count must be a whole number.'}, status=status.HTTP_400_BAD_REQUEST)

        if not MIN_RETAIN_COUNT <= retain_count <= MAX_RETAIN_COUNT:
            return Response(
                {'detail': f'retain_count must be between {MIN_RETAIN_COUNT} and {MAX_RETAIN_COUNT}.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if frequency not in BACKUP_FREQUENCIES:
            return Response({'detail': 'Invalid frequency. Use hourly, daily, weekly, or monthly.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            datetime.strptime(at_time, '%H:%M')
        except ValueError:
            return Response({'detail': 'Invalid time format. Use HH:MM.'}, status=status.HTTP_400_BAD_REQUEST)

        schedule = self._load_schedule()
        schedule.update(
            {
                'enabled': enabled,
                'automatic': automatic,
                'frequency': frequency,
                'time': at_time,
                'retain_count': retain_count,
            }
        )

        if enabled and automatic:
            schedule['next_backup_at'] = self._calculate_next_run(frequency, at_time).isoformat()
        else:
            schedule['next_backup_at'] = None

        self._save_schedule(schedule)
        self._append_event('SCHEDULE_UPDATED', 'Backup schedule updated.', actor=request.user)
        return Response({'detail': 'Backup schedule updated.', 'schedule': schedule})

    @action(detail=False, methods=['post'], url_path='restore_backup')
    def restore_backup(self, request):
        denied = self._ensure_manager(request)
        if denied:
            return denied

        uploaded_file = request.FILES.get('file')
        if not uploaded_file:
            return Response({'detail': 'Backup file is required.'}, status=status.HTTP_400_BAD_REQUEST)

        if not uploaded_file.name.lower().endswith('.json'):
            return Response({'detail': 'Only JSON backup files are supported.'}, status=status.HTTP_400_BAD_REQUEST)

        if uploaded_file.size > MAX_RESTORE_UPLOAD_BYTES:
            return Response({'detail': 'Backup file is too large to restore.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            clear_existing = _parse_bool(request.query_params.get('clear_existing'), default=False)
        except ValueError:
            return Response({'detail': 'clear_existing must be true or false.'}, status=status.HTTP_400_BAD_REQUEST)

        # Validate the payload before touching the database.
        # ``loaddata`` infers the serializer from the extension, so keep ``.json``.
        temp_path = self.backup_dir / f"restore-{uuid.uuid4().hex}.json"
        try:
            with temp_path.open('wb') as destination:
                for chunk in uploaded_file.chunks():
                    destination.write(chunk)

            try:
                with temp_path.open('r', encoding='utf-8') as handle:
                    payload = json.load(handle)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return Response({'detail': 'The selected file is not a valid backup.'}, status=status.HTTP_400_BAD_REQUEST)
            if not isinstance(payload, (list, dict)):
                return Response({'detail': 'The selected file is not a valid backup.'}, status=status.HTTP_400_BAD_REQUEST)

            # Take a safety copy of the current data so a bad restore can be undone.
            safety_name = None
            if clear_existing:
                try:
                    safety_name = self._create_backup(prefix='pre-restore-backup')
                except Exception as exc:
                    logger.exception('Pre-restore backup failed')
                    self._append_event('RESTORE', f'Restore aborted: safety backup failed ({exc})', actor=request.user, outcome='FAILED')
                    return Response(
                        {'detail': 'Restore aborted because a safety backup of the current data could not be created.'},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    )

            try:
                call_command('restore_system', str(temp_path), clear=clear_existing)
            except Exception as exc:
                logger.exception('Restore failed')
                self._append_event('RESTORE', f'Restore failed: {exc}', actor=request.user, outcome='FAILED')
                return Response({'detail': f'Restore failed: {exc}'}, status=status.HTTP_400_BAD_REQUEST)
        finally:
            temp_path.unlink(missing_ok=True)

        message = 'Backup restored successfully.'
        if safety_name:
            message += f' A safety copy of the previous data was saved as {safety_name}.'
        self._append_event('RESTORE', message, actor=request.user, file_name=uploaded_file.name)
        return Response({'detail': message, 'safety_backup': safety_name})

    @action(detail=False, methods=['get'], url_path='download')
    def download(self, request):
        denied = self._ensure_manager(request)
        if denied:
            return denied

        file_name = request.query_params.get('file')
        if not file_name:
            return Response({'detail': 'Query parameter "file" is required.'}, status=status.HTTP_400_BAD_REQUEST)

        requested = Path(file_name).name
        file_path = self.backup_dir / requested
        if not self._is_backup_file(file_path):
            return Response({'detail': 'Backup file not found.'}, status=status.HTTP_404_NOT_FOUND)

        self._append_event('DOWNLOAD', f'Backup downloaded: {requested}', actor=request.user, file_name=requested)

        return FileResponse(file_path.open('rb'), as_attachment=True, filename=file_path.name)