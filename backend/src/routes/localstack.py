import base64
import uuid

from flask import Blueprint
from flask import jsonify
from flask import redirect
from flask import render_template
from flask import request
from flask import url_for
from sqlalchemy.exc import IntegrityError

from src import utils as u
from src.models import UploadTask, db
from src.settings import Settings as S
from src.tasks import Tasks


blueprint = Blueprint('aws', __name__)


@blueprint.route('/api/v1/files/')
def files():
    s3 = u.s3_client()
    u.ensure_bucket(s3)
    return jsonify(s3.list_objects(Bucket=S.S3_BUCKET))


@blueprint.route('/api/v1/files/upload/', methods=['GET', 'POST'])
def upload():
    if request.method != 'POST':
        return jsonify({'message': 'POST a file to upload'})

    if 'file' not in request.files:
        return jsonify({'message': 'no file'}), 400
    file = request.files['file']
    if not file:
        return jsonify({'message': 'no file'}), 400

    filename = file.filename
    idempotency_key = (request.headers.get('X-Idempotency-Key')
                       or request.form.get('idempotency_key')
                       or str(uuid.uuid4()))

    if S.UPLOAD_MODE == 'sync':
        return _upload_sync(idempotency_key, filename, file.read())
    return _upload_async(idempotency_key, filename, file.read())


def _upload_sync(idempotency_key, filename, content):
    """Inline upload: the request blocks until S3 has the object."""
    try:
        result = u.perform_upload(idempotency_key, filename, content)
    except Exception as exc:
        return jsonify(error=f'upload failed: {exc}'), 503
    return jsonify(message=result['key'], mode='sync', result=result)


def _upload_async(idempotency_key, filename, content):
    """Enqueue a Celery upload and track it via the UploadTask state machine."""
    # Decision: fail fast with 503 instead of polling until localstack is up.
    if not u.localstack_ready():
        return jsonify(error='localstack is not ready, try again later'), 503

    existing = UploadTask.get(idempotency_key=idempotency_key)
    if existing:
        return jsonify(task_id=existing.task_id,
                       status=existing.status,
                       deduplicated=True), 200

    task_id = str(uuid.uuid4())
    try:
        record = UploadTask.create(task_id=task_id,
                                   idempotency_key=idempotency_key,
                                   filename=filename,
                                   status=UploadTask.STATUS_PENDING)
    except IntegrityError:
        # Lost a race against a concurrent submission of the same key.
        db.session.rollback()
        existing = UploadTask.get(idempotency_key=idempotency_key)
        return jsonify(task_id=existing.task_id,
                       status=existing.status,
                       deduplicated=True), 200

    try:
        Tasks.upload_file.apply_async(
            args=[task_id, idempotency_key, filename,
                  base64.b64encode(content).decode('ascii')],
            task_id=task_id)
    except Exception as exc:
        record.advance(UploadTask.STATUS_FAILED,
                       error=f'dispatch failed: {exc}')
        return jsonify(error='failed to enqueue upload',
                       task_id=task_id), 503

    return jsonify(task_id=task_id,
                   status=UploadTask.STATUS_PENDING,
                   status_url=url_for('aws.status_page'),
                   task_url=url_for('aws.task_detail', task_id=task_id)), 202


@blueprint.route('/api/v1/tasks/')
def tasks():
    return jsonify([task.to_dict() for task in
                    UploadTask.query.order_by(UploadTask.id.desc()).all()])


@blueprint.route('/api/v1/tasks/<task_id>')
def task_detail(task_id):
    task = UploadTask.get(task_id=task_id)
    if not task:
        return jsonify(error='not found'), 404
    return jsonify(task.to_dict())


@blueprint.route('/status')
def status_page():
    tasks = UploadTask.query.order_by(UploadTask.id.desc()).all()
    return render_template('status.html', tasks=tasks)


@blueprint.route('/api/v1/files/create/')
def add_file():
    s3 = u.s3_client()
    u.ensure_bucket(s3)
    s3.put_object(Bucket=S.S3_BUCKET,
                  Key='{}.txt'.format(uuid.uuid1()),
                  Body=b'some content')
    return redirect(url_for('.files'))
