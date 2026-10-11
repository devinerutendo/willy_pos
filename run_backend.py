import argparse
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

if getattr(sys, 'frozen', False):
    # Inside a PyInstaller onedir bundle, __file__ resolves relative to the
    # extracted/internal bundle directory, not the folder containing the
    # actual .exe. Use sys.executable so .env is found next to the exe.
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / '.env'


def _run_manage(*args: str) -> None:
    command = [sys.executable, 'manage.py', *args]
    result = subprocess.run(command, cwd=BASE_DIR, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(command)}")


def _load_env() -> None:
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)


def _test_db_from_args(args: argparse.Namespace) -> None:
    import psycopg2

    conn = psycopg2.connect(
        dbname=args.admin_db,
        user=args.admin_user,
        password=args.admin_password,
        host=args.db_host,
        port=int(args.db_port),
        connect_timeout=10,
    )
    conn.close()
    print('DB_AUTH_OK')


def cmd_check_env(_args: argparse.Namespace) -> None:
    _load_env()
    required = ['DB_NAME', 'DB_USER', 'DB_PASSWORD', 'DB_HOST', 'DB_PORT']
    missing = [key for key in required if not os.getenv(key, '').strip()]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
    print('ENV_OK')


def cmd_test_db(args: argparse.Namespace) -> None:
    _test_db_from_args(args)


def cmd_migrate(_args: argparse.Namespace) -> None:
    _load_env()
    from scripts.bootstrap_database import ensure_licensing_tables, ensure_role_and_database, run_django_setup

    ensure_role_and_database()
    run_django_setup()
    ensure_licensing_tables()
    print('MIGRATE_OK')


def cmd_serve(_args: argparse.Namespace) -> None:
    _load_env()
    from scripts.bootstrap_database import ensure_licensing_tables, ensure_role_and_database, ensure_bootstrap_superuser, run_django_setup

    ensure_role_and_database()
    run_django_setup()
    ensure_licensing_tables()
    ensure_bootstrap_superuser()

    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'Core.settings')
    from django.core.wsgi import get_wsgi_application
    from waitress import serve

    host = os.getenv('DJANGO_HOST', '127.0.0.1')
    port = int(os.getenv('DJANGO_PORT', '8000'))
    print(f'Starting backend server on {host}:{port}')
    serve(get_wsgi_application(), host=host, port=port)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='WillyPOS backend runtime dispatcher')
    subparsers = parser.add_subparsers(dest='command', required=True)

    serve_parser = subparsers.add_parser('serve', help='Bootstrap and run the backend server')
    serve_parser.set_defaults(func=cmd_serve)

    migrate_parser = subparsers.add_parser('migrate', help='Bootstrap DB role/database and run migrations')
    migrate_parser.set_defaults(func=cmd_migrate)

    env_parser = subparsers.add_parser('check-env', help='Validate required environment variables')
    env_parser.set_defaults(func=cmd_check_env)

    test_db_parser = subparsers.add_parser('test-db', help='Test PostgreSQL admin credentials and connectivity')
    test_db_parser.add_argument('--db-host', required=True)
    test_db_parser.add_argument('--db-port', required=True)
    test_db_parser.add_argument('--admin-user', required=True)
    test_db_parser.add_argument('--admin-password', required=True)
    test_db_parser.add_argument('--admin-db', required=True)
    test_db_parser.set_defaults(func=cmd_test_db)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
