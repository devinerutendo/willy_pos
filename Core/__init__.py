"""
Core package initialization - loads Celery on Django startup.
"""

# Import Celery app to ensure it runs with Django
from .celery import app as celery_app

__all__ = ['celery_app']
