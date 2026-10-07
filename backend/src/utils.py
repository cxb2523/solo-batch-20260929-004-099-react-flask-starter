import socket
import time

import localstack_client.session
from botocore.config import Config
from botocore.exceptions import ClientError
from werkzeug.utils import secure_filename

from src.settings import Settings as S


def wait_for_service(host, port, timeout=5.0):
    """Wait until a port starts accepting TCP connections.
    Args:
        port (int): Port number.
        host (str): Host address on which the port should exist.
        timeout (float): In seconds. How long to wait before raising errors.
    Raises:
        TimeoutError: The port isn't accepting connection after time specified in `timeout`.
    """
    start_time = time.perf_counter()
    while True:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                break
        except OSError as ex:
            time.sleep(0.01)
            if time.perf_counter() - start_time >= timeout:
                raise TimeoutError('Waited too long for the port {} on host {} to start accepting '
                                   'connections.'.format(port, host)) from ex


def s3_client(timeout=None):
    """Build an S3 client pointed at localstack.

    When `timeout` is given the client fails fast (no retries), which is
    used for the readiness probe so a down localstack surfaces quickly.
    """
    config = None
    if timeout is not None:
        config = Config(connect_timeout=timeout,
                        read_timeout=timeout,
                        retries={'max_attempts': 0})
    return localstack_client.session.Session().client('s3', config=config)


def localstack_ready(timeout=None):
    """Short probe: is localstack S3 answering right now?"""
    try:
        s3_client(timeout=timeout or S.S3_HEALTHCHECK_TIMEOUT).list_buckets()
        return True
    except Exception:
        return False


def ensure_bucket(s3):
    try:
        s3.create_bucket(Bucket=S.S3_BUCKET)
    except ClientError as exc:
        code = exc.response.get('Error', {}).get('Code')
        if code not in ('BucketAlreadyOwnedByYou', 'BucketAlreadyExists'):
            raise


def s3_key_for(idempotency_key, filename):
    """The S3 key is derived from the idempotency key only, so retries and
    duplicate submissions of the same logical upload always converge on a
    single object."""
    name = secure_filename(filename) or 'file'
    return f'uploads/{idempotency_key}/{name}'


def perform_upload(idempotency_key, filename, content):
    """Idempotently upload `content` to S3.

    Returns a result dict. If the object already exists the put is skipped,
    so calling this any number of times with the same idempotency key leaves
    exactly one object in the bucket.
    """
    s3 = s3_client()
    ensure_bucket(s3)
    key = s3_key_for(idempotency_key, filename)
    try:
        s3.head_object(Bucket=S.S3_BUCKET, Key=key)
        return {'bucket': S.S3_BUCKET, 'key': key, 'skipped': True}
    except ClientError as exc:
        code = exc.response.get('Error', {}).get('Code')
        if code not in ('404', 'NoSuchKey', 'NotFound'):
            raise
    response = s3.put_object(Bucket=S.S3_BUCKET, Key=key, Body=content)
    return {'bucket': S.S3_BUCKET,
            'key': key,
            'etag': response.get('ETag'),
            'skipped': False}
