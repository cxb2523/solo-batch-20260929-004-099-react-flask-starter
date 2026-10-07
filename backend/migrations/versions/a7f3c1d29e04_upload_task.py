"""Add UploadTask model

Revision ID: a7f3c1d29e04
Revises: fb060a24643d
Create Date: 2026-10-05 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a7f3c1d29e04'
down_revision = 'fb060a24643d'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'upload_task',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('uuid', sa.String(length=36), nullable=True),
        sa.Column('task_id', sa.String(length=36), nullable=False),
        sa.Column('idempotency_key', sa.String(length=255), nullable=False),
        sa.Column('filename', sa.String(), nullable=False),
        sa.Column('s3_key', sa.String(), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('result', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('task_id'),
        sa.UniqueConstraint('idempotency_key'),
    )


def downgrade():
    op.drop_table('upload_task')
