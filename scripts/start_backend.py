import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = BASE_DIR / '.env'


def run(command: list[str]) -> None:
    result = subprocess.run(command, cwd=BASE_DIR, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(command)}")


def main() -> None:
    load_dotenv(ENV_PATH)

    python_executable = sys.executable

    run([python_executable, 'scripts/bootstrap_database.py'])

    host = os.getenv('DJANGO_HOST', '127.0.0.1')
    port = os.getenv('DJANGO_PORT', '8000')

    run([python_executable, 'manage.py', 'runserver', f'{host}:{port}', '--noreload'])


if __name__ == '__main__':
    main()
