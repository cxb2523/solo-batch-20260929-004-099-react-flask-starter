import os


class Parse:
    """Class of utility functions to parse environment variables"""

    @staticmethod
    def bool(field):
        """Parse booleans (defaults to False)"""
        return os.getenv(field, '').lower() in ['true', '1']


class Settings:

    # General
    DEV = Parse.bool('DEV')
    REDIS_HOST = os.getenv('REDIS_HOST', 'redis')
    REDIS_URL = f'redis://{REDIS_HOST}:6379'

    # Upload pipeline
    # 'celery' -> enqueue an async upload task, 'sync' -> upload inline in the request
    UPLOAD_MODE = os.getenv('UPLOAD_MODE', 'celery')
    S3_BUCKET = os.getenv('S3_BUCKET', 'test-bucket')
    LOCALSTACK_HOST = os.getenv('LOCALSTACK_HOST', 'localhost')
    LOCALSTACK_PORT = os.getenv('LOCALSTACK_PORT', '4566')
    # Fail-fast probe timeout (seconds) used before accepting an upload
    S3_HEALTHCHECK_TIMEOUT = float(os.getenv('S3_HEALTHCHECK_TIMEOUT', '1.0'))
    # Celery retry policy for the upload task
    UPLOAD_MAX_RETRIES = int(os.getenv('UPLOAD_MAX_RETRIES', '3'))
    UPLOAD_RETRY_BACKOFF = int(os.getenv('UPLOAD_RETRY_BACKOFF', '2'))

    # Database
    AES_SECRET_KEY = os.getenv('AES_SECRET_KEY', 'fake-aes-key')
    SQLALCHEMY_DATABASE_URI = os.getenv('DATABASE_URL')

    # Celery
    CELERY_BROKER_URL = REDIS_URL
    CELERY_RESULT_BACKEND = REDIS_URL
    CELERY_CONFIG = {
        'beat_schedule': {
            'ping_once': {
                'task': 'src.tasks.ping_once',
                'args': (1,),
                'schedule': 5.0,
            },
        }
    }
