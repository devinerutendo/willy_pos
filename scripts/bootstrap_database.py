import os
import sys
from pathlib import Path
import uuid

import psycopg2
from dotenv import load_dotenv
from psycopg2 import sql


if getattr(sys, 'frozen', False):
    # Inside a PyInstaller onedir bundle, __file__ resolves relative to the
    # extracted/internal bundle directory, not the folder containing the
    # actual .exe. Use sys.executable so .env is found next to the exe.
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = BASE_DIR / '.env'


def get_env(name: str, default: str = '') -> str:
    value = os.getenv(name, default).strip()
    if not value:
        raise RuntimeError(f'Missing required environment variable: {name}')
    return value


def try_connect(dbname: str, user: str, password: str, host: str, port: int):
    return psycopg2.connect(
        dbname=dbname,
        user=user,
        password=password,
        host=host,
        port=port,
        connect_timeout=10,
    )


def ensure_role_and_database() -> None:
    db_name = get_env('DB_NAME')
    app_user = get_env('DB_USER')
    app_password = get_env('DB_PASSWORD')
    host = get_env('DB_HOST')
    port = int(get_env('DB_PORT'))

    admin_user = os.getenv('DB_ADMIN_USER', app_user).strip() or app_user
    admin_password = os.getenv('DB_ADMIN_PASSWORD', app_password).strip() or app_password
    admin_db = os.getenv('DB_ADMIN_DB', 'postgres').strip() or 'postgres'

    # If the application user can already connect to the target database,
    # provisioning is complete and admin credentials are no longer needed.
    try:
        app_conn = try_connect(db_name, app_user, app_password, host, port)
        app_conn.close()
        print(f'[BOOTSTRAP] Application database is reachable as {app_user}; skipping role/database provisioning.')
        return
    except psycopg2.OperationalError:
        pass

    try:
        conn = try_connect(admin_db, admin_user, admin_password, host, port)
    except psycopg2.OperationalError as exc:
        if admin_user == app_user and admin_password != app_password:
            try:
                conn = try_connect(admin_db, admin_user, app_password, host, port)
                print(f'[BOOTSTRAP] Admin user {admin_user} accepted the application password; continuing with unified credentials.')
            except psycopg2.OperationalError:
                raise RuntimeError(
                    f'Unable to authenticate with PostgreSQL admin user {admin_user}. '
                    'Check DB_ADMIN_PASSWORD, or use a distinct application user instead of postgres.'
                ) from exc
        else:
            raise RuntimeError(
                f'Unable to authenticate with PostgreSQL admin user {admin_user}. '
                'Check DB_ADMIN_PASSWORD and PostgreSQL authentication settings.'
            ) from exc
    conn.autocommit = True

    with conn.cursor() as cur:
        cur.execute('SELECT 1 FROM pg_roles WHERE rolname = %s', (app_user,))
        role_exists = cur.fetchone() is not None

        if not role_exists:
            cur.execute(
                sql.SQL('CREATE ROLE {} WITH LOGIN PASSWORD %s').format(sql.Identifier(app_user)),
                (app_password,),
            )
            print(f'[BOOTSTRAP] Created role: {app_user}')
        else:
            cur.execute(
                sql.SQL('ALTER ROLE {} WITH LOGIN PASSWORD %s').format(sql.Identifier(app_user)),
                (app_password,),
            )
            print(f'[BOOTSTRAP] Updated password for role: {app_user}')

        cur.execute('SELECT 1 FROM pg_database WHERE datname = %s', (db_name,))
        db_exists = cur.fetchone() is not None

        if not db_exists:
            cur.execute(
                sql.SQL('CREATE DATABASE {} OWNER {}').format(
                    sql.Identifier(db_name),
                    sql.Identifier(app_user),
                )
            )
            print(f'[BOOTSTRAP] Created database: {db_name}')
        else:
            print(f'[BOOTSTRAP] Database already exists: {db_name}')

        cur.execute(
            sql.SQL('GRANT ALL PRIVILEGES ON DATABASE {} TO {}').format(
                sql.Identifier(db_name),
                sql.Identifier(app_user),
            )
        )

    conn.close()


def _ensure_django_ready() -> None:
    if str(BASE_DIR) not in sys.path:
        sys.path.insert(0, str(BASE_DIR))

    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'Core.settings')

    import django
    django.setup()


def run_django_setup() -> None:
    # Run management commands in-process via call_command instead of
    # spawning `sys.executable manage.py ...`: in the frozen PyInstaller exe,
    # sys.executable is willypos-backend.exe itself (not a Python
    # interpreter) and manage.py isn't bundled as a data file, so the
    # subprocess approach fails silently in packaged builds.
    _ensure_django_ready()

    from django.core.management import call_command

    run_makemigrations = os.getenv('RUN_MAKEMIGRATIONS_ON_STARTUP', 'False').lower() == 'true'

    if run_makemigrations:
        print('[BOOTSTRAP] Running: makemigrations --noinput')
        call_command('makemigrations', interactive=False)

    print('[BOOTSTRAP] Running: migrate --noinput')
    call_command('migrate', interactive=False)


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {'1', 'true', 'yes', 'on'}


def ensure_bootstrap_superuser() -> None:
    if not _env_bool('AUTO_CREATE_SUPERUSER', False):
        print('[BOOTSTRAP] AUTO_CREATE_SUPERUSER disabled; skipping superuser bootstrap.')
        return

    _ensure_django_ready()

    from django.contrib.auth import get_user_model

    user_model = get_user_model()

    if user_model.objects.filter(is_superuser=True).exists():
        print('[BOOTSTRAP] Superuser already exists; skipping superuser bootstrap.')
        return

    email = os.getenv('BOOTSTRAP_SUPERUSER_EMAIL', '').strip()
    password = os.getenv('BOOTSTRAP_SUPERUSER_PASSWORD', '').strip()
    first_name = os.getenv('BOOTSTRAP_SUPERUSER_FIRST_NAME', 'System').strip() or 'System'
    last_name = os.getenv('BOOTSTRAP_SUPERUSER_LAST_NAME', 'Administrator').strip() or 'Administrator'
    username = os.getenv('BOOTSTRAP_SUPERUSER_USERNAME', '').strip()

    if not email or not password:
        raise RuntimeError(
            'AUTO_CREATE_SUPERUSER is enabled but BOOTSTRAP_SUPERUSER_EMAIL or '
            'BOOTSTRAP_SUPERUSER_PASSWORD is missing.'
        )

    if not username:
        local_part = email.split('@', 1)[0] if '@' in email else email
        candidate = local_part or 'admin'
        if user_model.objects.filter(username=candidate).exists():
            candidate = f'{candidate}_{uuid.uuid4().hex[:6]}'
        username = candidate

    user = user_model.objects.create_superuser(
        email=email,
        username=username,
        first_name=first_name,
        last_name=last_name,
        password=password,
    )

    if hasattr(user, 'role') and hasattr(user_model, 'ADMIN'):
        user.role = user_model.ADMIN
        user.save(update_fields=['role'])

    print(f'[BOOTSTRAP] Created bootstrap superuser: {email}')


def ensure_licensing_tables() -> None:
    """
    Guard against migration-recorder drift where Licensing migrations are marked
    as applied but the physical tables are missing (seen on some first-run DBs).
    """
    db_name = get_env('DB_NAME')
    app_user = get_env('DB_USER')
    app_password = get_env('DB_PASSWORD')
    host = get_env('DB_HOST')
    port = int(get_env('DB_PORT'))

    required_tables = {
        'licensing_license',
        'licensing_machine_registration',
    }

    conn = try_connect(db_name, app_user, app_password, host, port)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = ANY(%s)
                """,
                (list(required_tables),),
            )
            existing = {row[0] for row in cur.fetchall()}
    finally:
        conn.close()

    missing = sorted(required_tables - existing)
    if not missing:
        print('[BOOTSTRAP] Licensing schema verified.')
        return

    print(f"[BOOTSTRAP] Missing Licensing tables detected: {', '.join(missing)}")
    print('[BOOTSTRAP] Rebuilding Licensing migrations to heal schema state...')

    _ensure_django_ready()

    from django.core.management import call_command

    print('[BOOTSTRAP] Running: migrate Licensing zero --fake --noinput')
    call_command('migrate', 'Licensing', 'zero', fake=True, interactive=False)

    print('[BOOTSTRAP] Running: migrate Licensing --noinput')
    call_command('migrate', 'Licensing', interactive=False)

    print('[BOOTSTRAP] Licensing schema recovery completed.')


def main() -> None:
    load_dotenv(ENV_PATH)

    print('[BOOTSTRAP] Starting database bootstrap...')
    ensure_role_and_database()
    run_django_setup()
    ensure_licensing_tables()
    ensure_bootstrap_superuser()
    print('[BOOTSTRAP] Database bootstrap completed successfully.')


if __name__ == '__main__':
    main()
