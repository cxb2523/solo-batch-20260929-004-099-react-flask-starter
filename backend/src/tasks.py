import base64
import json

from celery import Celery
from celery.exceptions import MaxRetriesExceededError

from src import utils as u
from src.models import Counter, UploadTask
from src.settings import Settings as S


def make_celery(app):
    # Create and configure the celery app
    celery = Celery(
        app.import_name,
        backend=app.config['CELERY_RESULT_BACKEND'],
        broker=app.config['CELERY_BROKER_URL']
    )
    celery.conf.update(**S.CELERY_CONFIG)

    # Subclass the base Task so that tasks run in a Flask app context
    TaskBase = celery.Task
    class ContextTask(TaskBase):  # noqa
        def __call__(self, *args, **kwargs):  # noqa
            with app.app_context():
                return TaskBase.__call__(self, *args, **kwargs)
    celery.Task = ContextTask

    # Add the tasks
    Tasks.bind(celery)

    return celery


class Tasks:

    # How to add new tasks:
    # - Create the static method (like `ping_once()`)
    # - Add the corresponding bind in the bind() method

    @classmethod
    def bind(cls, celery):
        """Bind the Celery app to all the tasks"""
        # NOTE: DONT
        cls.ping_once = celery.task(cls.ping_once)
        cls.upload_file = celery.task(
            bind=True,
            max_retries=S.UPLOAD_MAX_RETRIES,
            retry_backoff=S.UPLOAD_RETRY_BACKOFF,
            retry_backoff_max=60,
            retry_jitter=False,
        )(cls.upload_file)

    @staticmethod
    def ping_once(amount):
        for counter in Counter.list():
            counter.increment(amount=amount)

    @staticmethod
    def upload_file(task, task_id, idempotency_key, filename, content_b64):
        """Upload a file to S3, tracking progress on the UploadTask row.

        Idempotent: the S3 key is derived from `idempotency_key`, so retries
        and duplicate deliveries never create a second object. The row only
        ever moves forward: pending -> running -> done/failed.
        """
        record = UploadTask.get(task_id=task_id, exception=True)
        if record.status == UploadTask.STATUS_DONE:
            return json.loads(record.result) if record.result else None

        record.advance(UploadTask.STATUS_RUNNING,
                       attempts=task.request.retries + 1)
        try:
            content = base64.b64decode(content_b64)
            result = u.perform_upload(idempotency_key, filename, content)
        except Exception as exc:
            try:
                task.retry(exc=exc)
            except MaxRetriesExceededError:
                record.advance(UploadTask.STATUS_FAILED, error=str(exc))
            return

        record.advance(UploadTask.STATUS_DONE,
                       s3_key=result['key'],
                       result=json.dumps(result),
                       error=None)
        return result
