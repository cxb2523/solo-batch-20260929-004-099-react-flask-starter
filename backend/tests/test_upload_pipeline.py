import base64
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError
from celery.exceptions import MaxRetriesExceededError, Retry
from sqlalchemy.exc import IntegrityError

from src import utils as u
from src.models import UploadTask, db
from src.settings import Settings as S
from src.tasks import Tasks


def make_record(**kwargs):
    defaults = dict(task_id='task-1',
                    idempotency_key='key-1',
                    filename='a.txt',
                    status=UploadTask.STATUS_PENDING)
    defaults.update(kwargs)
    return UploadTask.create(**defaults)


class FakeS3:
    """In-memory stand-in for the localstack S3 client."""

    def __init__(self):
        self.objects = {}
        self.put_count = 0

    def create_bucket(self, Bucket):
        return {}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError(
                {'Error': {'Code': '404', 'Message': 'Not Found'}},
                'HeadObject')
        return {}

    def put_object(self, Bucket, Key, Body):
        self.put_count += 1
        self.objects[Key] = Body
        return {'ETag': '"fake-etag"'}


class FakeTaskSelf:
    """Stands in for the bound celery task instance."""

    def __init__(self, retries=0, retry_exc=None):
        self.request = SimpleNamespace(retries=retries)
        self.retry_calls = 0
        self._retry_exc = retry_exc or Retry()

    def retry(self, exc=None):
        self.retry_calls += 1
        raise self._retry_exc


def post_file(client, key=None):
    data = {'file': (io.BytesIO(b'hello'), 'hello.txt')}
    if key:
        data['idempotency_key'] = key
    return client.post('/api/v1/files/upload/',
                       data=data,
                       content_type='multipart/form-data')


class TestStateMachine:

    def test_happy_path(self, app):
        record = make_record()
        assert record.advance(UploadTask.STATUS_RUNNING)
        assert record.advance(UploadTask.STATUS_DONE, s3_key='k')
        assert record.status == UploadTask.STATUS_DONE
        assert record.s3_key == 'k'

    def test_done_never_moves_backwards(self, app):
        record = make_record()
        record.advance(UploadTask.STATUS_RUNNING)
        record.advance(UploadTask.STATUS_DONE)
        assert not record.advance(UploadTask.STATUS_RUNNING)
        assert not record.advance(UploadTask.STATUS_PENDING)
        assert not record.advance(UploadTask.STATUS_FAILED)
        assert record.status == UploadTask.STATUS_DONE

    def test_failed_is_terminal(self, app):
        record = make_record()
        record.advance(UploadTask.STATUS_RUNNING)
        record.advance(UploadTask.STATUS_FAILED, error='boom')
        assert not record.advance(UploadTask.STATUS_RUNNING)
        assert not record.advance(UploadTask.STATUS_DONE)
        assert record.status == UploadTask.STATUS_FAILED
        assert record.error == 'boom'

    def test_running_to_running_is_allowed_for_retries(self, app):
        record = make_record()
        assert record.advance(UploadTask.STATUS_RUNNING, attempts=1)
        assert record.advance(UploadTask.STATUS_RUNNING, attempts=2)
        assert record.attempts == 2

    def test_pending_can_jump_to_terminal(self, app):
        record = make_record()
        assert record.advance(UploadTask.STATUS_FAILED, error='dispatch failed')
        assert record.status == UploadTask.STATUS_FAILED

    def test_unknown_status_rejected(self, app):
        record = make_record()
        with pytest.raises(ValueError):
            record.advance('sideways')

    def test_duplicate_idempotency_key_rejected(self, app):
        make_record()
        with pytest.raises(IntegrityError):
            make_record(task_id='task-2')
        db.session.rollback()


class TestPerformUpload:

    def test_same_idempotency_key_lands_one_object(self, app, monkeypatch):
        fake = FakeS3()
        monkeypatch.setattr(u, 's3_client', lambda timeout=None: fake)
        first = u.perform_upload('idem-1', 'a.txt', b'data')
        second = u.perform_upload('idem-1', 'a.txt', b'data')
        assert not first['skipped']
        assert second['skipped']
        assert first['key'] == second['key']
        assert fake.put_count == 1
        assert list(fake.objects) == [first['key']]

    def test_key_is_derived_from_idempotency_key(self, app):
        key_a = u.s3_key_for('idem-1', 'a.txt')
        key_b = u.s3_key_for('idem-1', 'a.txt')
        key_c = u.s3_key_for('idem-2', 'a.txt')
        assert key_a == key_b
        assert key_a != key_c
        assert 'idem-1' in key_a


class TestUploadTask:

    def test_success_marks_done(self, app, monkeypatch):
        fake = FakeS3()
        monkeypatch.setattr(u, 's3_client', lambda timeout=None: fake)
        make_record()
        result = Tasks.upload_file(FakeTaskSelf(), 'task-1', 'key-1', 'a.txt',
                                   base64.b64encode(b'data').decode('ascii'))
        record = UploadTask.get(task_id='task-1')
        assert record.status == UploadTask.STATUS_DONE
        assert record.attempts == 1
        assert record.s3_key == result['key']
        assert json.loads(record.result)['key'] == result['key']
        assert fake.objects[result['key']] == b'data'

    def test_retry_keeps_status_running(self, app, monkeypatch):
        monkeypatch.setattr(u, 'perform_upload',
                            Mock(side_effect=RuntimeError('boom')))
        make_record()
        task = FakeTaskSelf(retries=0, retry_exc=Retry())
        with pytest.raises(Retry):
            Tasks.upload_file(task, 'task-1', 'key-1', 'a.txt', 'ZGF0YQ==')
        record = UploadTask.get(task_id='task-1')
        assert record.status == UploadTask.STATUS_RUNNING
        assert record.attempts == 1
        assert task.retry_calls == 1

    def test_exhausted_retries_marks_failed(self, app, monkeypatch):
        monkeypatch.setattr(u, 'perform_upload',
                            Mock(side_effect=RuntimeError('boom')))
        make_record()
        task = FakeTaskSelf(retries=3,
                            retry_exc=MaxRetriesExceededError('max retries'))
        Tasks.upload_file(task, 'task-1', 'key-1', 'a.txt', 'ZGF0YQ==')
        record = UploadTask.get(task_id='task-1')
        assert record.status == UploadTask.STATUS_FAILED
        assert 'boom' in record.error

    def test_done_record_short_circuits(self, app, monkeypatch):
        perform = Mock()
        monkeypatch.setattr(u, 'perform_upload', perform)
        make_record(status=UploadTask.STATUS_DONE,
                    result=json.dumps({'key': 'k'}))
        result = Tasks.upload_file(FakeTaskSelf(), 'task-1', 'key-1', 'a.txt',
                                   'ZGF0YQ==')
        assert result == {'key': 'k'}
        perform.assert_not_called()


class TestUploadRoute:

    def test_async_upload_enqueues_task(self, client, monkeypatch):
        monkeypatch.setattr(u, 'localstack_ready', lambda: True)
        apply_async = Mock()
        monkeypatch.setattr(Tasks, 'upload_file', Mock(apply_async=apply_async))
        resp = post_file(client, key='idem-42')
        assert resp.status_code == 202
        body = resp.get_json()
        assert body['status'] == UploadTask.STATUS_PENDING
        record = UploadTask.get(task_id=body['task_id'])
        assert record.idempotency_key == 'idem-42'
        assert record.filename == 'hello.txt'
        apply_async.assert_called_once()
        args, kwargs = apply_async.call_args
        assert kwargs['task_id'] == body['task_id']
        assert kwargs['args'][0] == body['task_id']
        assert base64.b64decode(kwargs['args'][3]) == b'hello'

    def test_duplicate_submission_enqueues_once(self, client, monkeypatch):
        monkeypatch.setattr(u, 'localstack_ready', lambda: True)
        apply_async = Mock()
        monkeypatch.setattr(Tasks, 'upload_file', Mock(apply_async=apply_async))
        first = post_file(client, key='dup')
        second = post_file(client, key='dup')
        assert first.status_code == 202
        assert second.status_code == 200
        assert second.get_json()['deduplicated']
        assert second.get_json()['task_id'] == first.get_json()['task_id']
        assert apply_async.call_count == 1
        assert len(UploadTask.list()) == 1

    def test_localstack_down_returns_503(self, client, monkeypatch):
        monkeypatch.setattr(u, 'localstack_ready', lambda: False)
        resp = post_file(client, key='down')
        assert resp.status_code == 503
        assert UploadTask.get(idempotency_key='down') is None

    def test_dispatch_failure_marks_failed(self, client, monkeypatch):
        monkeypatch.setattr(u, 'localstack_ready', lambda: True)
        broken = Mock(apply_async=Mock(side_effect=RuntimeError('broker down')))
        monkeypatch.setattr(Tasks, 'upload_file', broken)
        resp = post_file(client, key='broker')
        assert resp.status_code == 503
        record = UploadTask.get(idempotency_key='broker')
        assert record.status == UploadTask.STATUS_FAILED
        assert 'broker down' in record.error

    def test_sync_mode_uploads_inline(self, client, monkeypatch):
        monkeypatch.setattr(S, 'UPLOAD_MODE', 'sync')
        fake = FakeS3()
        monkeypatch.setattr(u, 's3_client', lambda timeout=None: fake)
        resp = post_file(client, key='sync-1')
        assert resp.status_code == 200
        assert resp.get_json()['mode'] == 'sync'
        assert len(fake.objects) == 1

    def test_missing_file_rejected(self, client):
        resp = client.post('/api/v1/files/upload/', data={})
        assert resp.status_code == 400


class TestStatusViews:

    def test_status_page_lists_tasks(self, client):
        make_record()
        resp = client.get('/status')
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert 'pending' in html
        assert 'a.txt' in html
        assert 'key-1' in html

    def test_task_detail_json(self, client):
        record = make_record()
        resp = client.get(f'/api/v1/tasks/{record.task_id}')
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['status'] == UploadTask.STATUS_PENDING
        assert body['idempotency_key'] == 'key-1'

    def test_task_detail_404(self, client):
        assert client.get('/api/v1/tasks/nope').status_code == 404

    def test_tasks_index_json(self, client):
        make_record()
        resp = client.get('/api/v1/tasks/')
        assert resp.status_code == 200
        assert len(resp.get_json()) == 1
