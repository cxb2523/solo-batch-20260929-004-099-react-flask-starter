import datetime
import json

from src.models.base import BaseModel, db


class Counter(BaseModel):

    count = db.Column(db.Integer, default=0, nullable=False)
    label = db.Column(db.String(), nullable=False)

    RESET_THRESHOLD = 1000000000

    def increment(self, amount=1):
        self.count += amount
        if self.count > self.RESET_THRESHOLD:
            self.count = 0
        self.save()

    def reset(self):
        self.count = 0
        self.save()


class UploadTask(BaseModel):
    """One logical file upload, tracked through a monotonic state machine:

        pending -> running -> done | failed

    `done` and `failed` are terminal. Transitions are applied with a
    conditional UPDATE so concurrent workers can never move a task
    backwards (e.g. a stale worker cannot flip `done` back to `running`).
    """

    STATUS_PENDING = 'pending'
    STATUS_RUNNING = 'running'
    STATUS_DONE = 'done'
    STATUS_FAILED = 'failed'

    TERMINAL_STATUSES = (STATUS_DONE, STATUS_FAILED)
    STATUS_RANK = {
        STATUS_PENDING: 0,
        STATUS_RUNNING: 1,
        STATUS_DONE: 2,
        STATUS_FAILED: 2,
    }

    task_id = db.Column(db.String(36), unique=True, nullable=False)
    idempotency_key = db.Column(db.String(255), unique=True, nullable=False)
    filename = db.Column(db.String(), nullable=False)
    s3_key = db.Column(db.String(), nullable=True)
    status = db.Column(db.String(16), nullable=False, default=STATUS_PENDING)
    attempts = db.Column(db.Integer(), nullable=False, default=0)
    error = db.Column(db.Text(), nullable=True)
    result = db.Column(db.Text(), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False,
                           default=datetime.datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False,
                           default=datetime.datetime.utcnow)

    @classmethod
    def _allowed_prior_statuses(cls, status):
        """Statuses from which `status` may be reached.

        Terminal statuses are never a valid source, and the rank may never
        decrease, so a transition can only move forward (or refresh the
        current non-terminal state, e.g. running -> running on retry).
        """
        rank = cls.STATUS_RANK[status]
        return [s for s, r in cls.STATUS_RANK.items()
                if s not in cls.TERMINAL_STATUSES and r <= rank]

    def advance(self, status, **fields):
        """Move to `status`, updating `fields`, only if the transition is
        forward. Returns True when the transition was applied."""
        if status not in self.STATUS_RANK:
            raise ValueError(f'unknown status: {status}')
        values = dict(status=status,
                      updated_at=datetime.datetime.utcnow(),
                      **fields)
        rows = db.session.query(UploadTask) \
            .filter(UploadTask.id == self.id,
                    UploadTask.status.in_(self._allowed_prior_statuses(status))) \
            .update(values, synchronize_session=False)
        db.session.commit()
        db.session.refresh(self)
        return rows == 1

    def to_dict(self):
        return {
            'task_id': self.task_id,
            'idempotency_key': self.idempotency_key,
            'filename': self.filename,
            's3_key': self.s3_key,
            'status': self.status,
            'attempts': self.attempts,
            'error': self.error,
            'result': json.loads(self.result) if self.result else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
